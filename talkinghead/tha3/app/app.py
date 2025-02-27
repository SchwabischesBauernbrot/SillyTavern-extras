"""THA3 live mode for SillyTavern-extras.

This is the animation engine, running on top of the THA3 posing engine.
This module implements the live animation backend and serves the API. For usage, see `server.py`.

If you want to play around with THA3 expressions in a standalone app, see `manual_poser.py`.
"""

import atexit
import io
import logging
import math
import os
import random
import sys
import time
import numpy as np
import threading
from typing import Dict, List, NoReturn, Optional, Union

import PIL

import torch

from flask import Flask, Response
from flask_cors import CORS

from tha3.poser.modes.load_poser import load_poser
from tha3.poser.poser import Poser
from tha3.util import (torch_linear_to_srgb, resize_PIL_image,
                       extract_PIL_image_from_filelike, extract_pytorch_image_from_PIL_image)
from tha3.app.postprocessor import Postprocessor
from tha3.app.util import posedict_keys, posedict_key_to_index, load_emotion_presets, posedict_to_pose, to_talkinghead_image, RunningAverage

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------------
# Global variables

talkinghead_basedir = "talkinghead"

global_animator_instance = None
_animator_output_lock = threading.Lock()  # protect from concurrent access to `result_image` and the `new_frame_available` flag.
global_encoder_instance = None
global_latest_frame_sent = None

# These need to be written to by the API functions.
#
# Since the plugin might not have been started yet at that time (so the animator instance might not exist),
# it's better to keep this state in module-level globals rather than in attributes of the animator.
animation_running = False  # used in initial bootup state, and while loading a new image
current_emotion = "neutral"
is_talking = False
global_reload_image = None

# --------------------------------------------------------------------------------
# API

# Flask setup
app = Flask(__name__)
CORS(app)

def setEmotion(_emotion: Dict[str, float]) -> None:
    """Set the current emotion of the character based on sentiment analysis results.

    Currently, we pick the emotion with the highest confidence score.

    The `set_emotion` API endpoint also uses this function to set the current emotion,
    with a manually formatted dictionary containing just one entry.

    _emotion: result of sentiment analysis: {emotion0: confidence0, ...}
    """
    global current_emotion

    highest_score = float("-inf")
    highest_label = None

    for item in _emotion:
        if item["score"] > highest_score:
            highest_score = item["score"]
            highest_label = item["label"]

    # Never triggered currently, because `setSpriteSlashCommand` at the client end (`SillyTavern/public/scripts/extensions/expressions/index.js`)
    # searches for a static sprite for the given expression, and does not proceed to `sendExpressionCall` if not found.
    # So beside `talkinghead.png`, your character also needs the static sprites for "/emote xxx" to work.
    if highest_label not in global_animator_instance.emotions:
        logger.warning(f"setEmotion: emotion '{highest_label}' does not exist, setting to 'neutral'")
        highest_label = "neutral"

    logger.info(f"setEmotion: applying emotion {highest_label}")
    current_emotion = highest_label
    return f"emotion set to {highest_label}"

def unload() -> str:
    """Stop animation."""
    global animation_running
    animation_running = False
    logger.info("unload: animation paused")
    return "Animation Paused"

def start_talking() -> str:
    """Start talking animation."""
    global is_talking
    is_talking = True
    logger.debug("start_talking called")
    return "started"

def stop_talking() -> str:
    """Stop talking animation."""
    global is_talking
    is_talking = False
    logger.debug("stop_talking called")
    return "stopped"

# There are three tasks we must do each frame:
#
#   1) Render an animation frame
#   2) Encode the new animation frame for network transport
#   3) Send the animation frame over the network
#
# Instead of running serially:
#
#   [render1][encode1][send1] [render2][encode2][send2]
# ------------------------------------------------------> time
#
# we get better throughput by parallelizing and interleaving:
#
#   [render1] [render2] [render3] [render4] [render5]
#             [encode1] [encode2] [encode3] [encode4]
#                       [send1]   [send2]   [send3]
# ----------------------------------------------------> time
#
# Despite the global interpreter lock, this increases throughput, as well as improves the timing of the network send
# since the network thread only needs to care about getting the send timing right.
#
# Either there's enough waiting for I/O for the split between render and encode to make a difference, or it's the fact
# that much of the compute-heavy work in both of those is performed inside C libraries that release the GIL (Torch,
# and the PNG encoder in Pillow, respectively).
#
# This is a simplified picture. Some important details:
#
#   - At startup:
#     - The animator renders the first frame on its own.
#     - The encoder waits for the animator to publish a frame, and then starts normal operation.
#     - The network thread waits for the encoder to publish a frame, and then starts normal operation.
#   - In normal operation (after startup):
#     - The animator waits until the encoder has consumed the previous published frame. Then it proceeds to render and publish a new frame.
#       - This communication is handled through the flag `animator.new_frame_available`.
#     - The network thread does its own thing on a regular schedule, based on the desired target FPS.
#       - However, the network thread publishes metadata on which frame is the latest that has been sent over the network at least once.
#         This is stored as an `id` (i.e. memory address) in `global_latest_frame_sent`.
#       - If the target FPS is too high for the animator and/or encoder to keep up with, the network thread re-sends
#         the latest frame published by the encoder as many times as necessary, to keep the network output at the target FPS
#         regardless of render/encode speed. This handles the case of hardware slower than the target FPS.
#       - On localhost, the network send is very fast, under 0.15 ms.
#     - The encoder uses the metadata to wait until the latest encoded frame has been sent at least once before publishing a new frame.
#       This ensures that no more frames are generated than are actually sent, and syncs also the animator (because the animator is
#       rate-limited by the encoder consuming its frames). This handles the case of hardware faster than the target FPS.
#     - When the animator and encoder are fast enough to keep up with the target FPS, generally when frame N is being sent,
#       frame N+1 is being encoded (or is already encoded, and waiting for frame N to be sent), and frame N+2 is being rendered.
#
def result_feed() -> Response:
    """Return a Flask `Response` that repeatedly yields the current image as 'image/png'."""
    def generate():
        global global_latest_frame_sent

        last_frame_send_complete_time = None
        last_report_time = None
        send_duration_sec = 0.0
        send_duration_statistics = RunningAverage()

        while True:
            # Send the latest available animation frame.
            # Important: grab reference to `image_bytes` only once, since it will be atomically updated without a lock.
            image_bytes = global_encoder_instance.image_bytes
            if image_bytes is not None:
                # How often should we send?
                #  - Excessive spamming can DoS the SillyTavern GUI, so there needs to be a rate limit.
                #  - OTOH, we must constantly send something, or the GUI will lock up waiting.
                TARGET_FPS = 25
                frame_duration_target_sec = 1 / TARGET_FPS
                if last_frame_send_complete_time is not None:
                    time_now = time.time_ns()
                    this_frame_elapsed_sec = (time_now - last_frame_send_complete_time) / 10**9
                    # The 2* is a fudge factor. It doesn't matter if the frame is a bit too early, but we don't want it to be late.
                    time_until_frame_deadline = frame_duration_target_sec - this_frame_elapsed_sec - 2 * send_duration_sec
                else:
                    time_until_frame_deadline = 0.0  # nothing rendered yet

                if time_until_frame_deadline <= 0.0:
                    time_now = time.time_ns()
                    yield (b"--frame\r\n"
                           b"Content-Type: image/png\r\n\r\n" + image_bytes + b"\r\n")
                    global_latest_frame_sent = id(image_bytes)  # atomic update, no need for lock
                    send_duration_sec = (time.time_ns() - time_now) / 10**9  # about 0.12 ms on localhost (compress_level=1 or 6, doesn't matter)
                    # print(f"send {send_duration_sec:0.6g}s")  # DEBUG

                    # Update the FPS counter, measuring the time between network sends.
                    time_now = time.time_ns()
                    if last_frame_send_complete_time is not None:
                        this_frame_elapsed_sec = (time_now - last_frame_send_complete_time) / 10**9
                        send_duration_statistics.add_datapoint(this_frame_elapsed_sec)
                    last_frame_send_complete_time = time_now
                else:
                    time.sleep(time_until_frame_deadline)

                # Log the FPS counter in 5-second intervals.
                time_now = time.time_ns()
                if animation_running and (last_report_time is None or time_now - last_report_time > 5e9):
                    avg_send_sec = send_duration_statistics.average()
                    msec = round(1000 * avg_send_sec, 1)
                    target_msec = round(1000 * frame_duration_target_sec, 1)
                    fps = round(1 / avg_send_sec, 1) if avg_send_sec > 0.0 else 0.0
                    logger.info(f"output: {msec:.1f}ms [{fps:.1f} FPS]; target {target_msec:.1f}ms [{TARGET_FPS:.1f} FPS]")
                    last_report_time = time_now

            else:  # first frame not yet available
                time.sleep(0.1)

    return Response(generate(), mimetype="multipart/x-mixed-replace; boundary=frame")

# TODO: the input is a flask.request.file.stream; what's the type of that?
def talkinghead_load_file(stream) -> str:
    """Load image from stream and start animation."""
    global global_reload_image
    global animation_running
    logger.info("talkinghead_load_file: loading new input image from stream")

    try:
        animation_running = False  # pause animation while loading a new image
        pil_image = PIL.Image.open(stream)  # Load the image using PIL.Image.open
        img_data = io.BytesIO()  # Create a copy of the image data in memory using BytesIO
        pil_image.save(img_data, format="PNG")
        global_reload_image = PIL.Image.open(io.BytesIO(img_data.getvalue()))  # Set the global_reload_image to a copy of the image data
    except PIL.Image.UnidentifiedImageError:
        logger.warning("Could not load input image from stream, loading blank")
        full_path = os.path.join(os.getcwd(), os.path.normpath(os.path.join(talkinghead_basedir, "tha3", "images", "inital.png")))
        global_reload_image = PIL.Image.open(full_path)
    finally:
        animation_running = True
    return "OK"

def launch(device: str, model: str) -> Union[None, NoReturn]:
    """Launch the talking head plugin (live mode).

    If the plugin fails to load, the process exits.

    device: "cpu" or "cuda"
    model: one of the folder names inside "talkinghead/tha3/models/"
    """
    global global_animator_instance
    global global_encoder_instance

    try:
        # If the animator already exists, clean it up first
        if global_animator_instance is not None:
            logger.info(f"launch: relaunching on device {device} with model {model}")
            global_animator_instance.exit()
            global_animator_instance = None
            global_encoder_instance.exit()
            global_encoder_instance = None

        poser = load_poser(model, device, modelsdir=os.path.join(talkinghead_basedir, "tha3", "models"))
        global_animator_instance = Animator(poser, device)
        global_encoder_instance = Encoder()

        # Load initial blank character image
        full_path = os.path.join(os.getcwd(), os.path.normpath(os.path.join(talkinghead_basedir, "tha3", "images", "inital.png")))
        global_animator_instance.load_image(full_path)

        global_animator_instance.start()
        global_encoder_instance.start()

    except RuntimeError as exc:
        logger.error(exc)
        sys.exit()

# --------------------------------------------------------------------------------
# Internal stuff

def convert_linear_to_srgb(image: torch.Tensor) -> torch.Tensor:
    """RGBA (linear) -> RGBA (SRGB), preserving the alpha channel."""
    rgb_image = torch_linear_to_srgb(image[0:3, :, :])
    return torch.cat([rgb_image, image[3:4, :, :]], dim=0)


class Animator:
    """uWu Waifu"""

    def __init__(self, poser: Poser, device: torch.device):
        self.poser = poser
        self.device = device

        self.reset_animation_state()

        self.postprocessor = Postprocessor(device)
        self.render_duration_statistics = RunningAverage()
        self.animator_thread = None

        self.source_image: Optional[torch.tensor] = None
        self.result_image: Optional[np.array] = None
        self.new_frame_available = False
        self.last_report_time = None

        self.emotions, self.emotion_names = load_emotion_presets(os.path.join("talkinghead", "emotions"))

    # --------------------------------------------------------------------------------
    # Management

    def start(self) -> None:
        """Start the animation thread."""
        self._terminated = False
        def animator_update():
            while not self._terminated:
                try:
                    self.render_animation_frame()
                except Exception as exc:
                    logger.error(exc)
                    raise  # let the animator stop so we won't spam the log
                time.sleep(0.01)  # rate-limit the renderer to 100 FPS maximum (this could be adjusted later)
        self.animator_thread = threading.Thread(target=animator_update, daemon=True)
        self.animator_thread.start()
        atexit.register(self.exit)

    def exit(self) -> None:
        """Terminate the animation thread.

        Called automatically when the process exits.
        """
        self._terminated = True
        self.animator_thread.join()
        self.animator_thread = None

    def reset_animation_state(self):
        """Reset character state trackers for all animation drivers."""
        self.current_pose = None

        self.last_emotion = None
        self.last_emotion_change_timestamp = None

        self.last_blink_timestamp = None
        self.blink_interval = None

        self.last_sway_target_timestamp = None
        self.last_sway_target_pose = None
        self.sway_interval = None

        self.breathing_epoch = time.time_ns()

    def load_image(self, file_path=None) -> None:
        """Load the image file at `file_path`, and replace the current character with it.

        Except, if `global_reload_image is not None`, use the global reload image data instead.
        In that case `file_path` is not used.

        When done, this always sets `global_reload_image` to `None`.
        """
        global global_reload_image

        try:
            if global_reload_image is not None:
                pil_image = global_reload_image
            else:
                pil_image = resize_PIL_image(
                    extract_PIL_image_from_filelike(file_path),
                    (self.poser.get_image_size(), self.poser.get_image_size()))

            w, h = pil_image.size

            if pil_image.size != (512, 512):
                logger.info("Resizing Char Card to work")
                pil_image = to_talkinghead_image(pil_image)

            w, h = pil_image.size

            if pil_image.mode != "RGBA":
                logger.error("load_image: image must have alpha channel")
                self.source_image = None
            else:
                self.source_image = extract_pytorch_image_from_PIL_image(pil_image) \
                    .to(self.device).to(self.poser.get_dtype())

        except Exception as exc:
            logger.error(f"load_image: {exc}")

        finally:
            global_reload_image = None

    # --------------------------------------------------------------------------------
    # Animation drivers

    def apply_emotion_to_pose(self, emotion_posedict: Dict[str, float], pose: List[float]) -> List[float]:
        """Copy all morphs except breathing from `emotion_posedict` to `pose`.

        If a morph does not exist in `emotion_posedict`, its value is copied from the original `pose`.

        Return the modified pose.
        """
        new_pose = list(pose)  # copy
        for idx, key in enumerate(posedict_keys):
            if key in emotion_posedict and key != "breathing_index":
                new_pose[idx] = emotion_posedict[key]
        return new_pose

    def animate_blinking(self, pose: List[float]) -> List[float]:
        """Eye blinking animation driver.

        Return the modified pose.
        """
        should_blink = (random.random() <= 0.03)

        # Prevent blinking too fast in succession.
        time_now = time.time_ns()
        if self.blink_interval is not None:
            # ...except when the "confusion" emotion has been entered recently.
            seconds_since_last_emotion_change = (time_now - self.last_emotion_change_timestamp) / 10**9
            if current_emotion == "confusion" and seconds_since_last_emotion_change < 10.0:
                pass
            else:
                seconds_since_last_blink = (time_now - self.last_blink_timestamp) / 10**9
                if seconds_since_last_blink < self.blink_interval:
                    should_blink = False

        if not should_blink:
            return pose

        # If there should be a blink, set the wink morphs to 1.
        new_pose = list(pose)  # copy
        for morph_name in ["eye_wink_left_index", "eye_wink_right_index"]:
            idx = posedict_key_to_index[morph_name]
            new_pose[idx] = 1.0

        # Typical for humans is 12...20 times per minute, i.e. 5...3 seconds interval.
        self.last_blink_timestamp = time_now
        self.blink_interval = random.uniform(2.0, 5.0)  # seconds; duration of this blink before the next one can begin

        return new_pose

    def animate_talking(self, pose: List[float]) -> List[float]:
        """Talking animation driver.

        Works by randomizing the mouth-open state.

        Return the modified pose.
        """
        if not is_talking:
            return pose

        # TODO: improve talking animation once we get the client to actually use it
        new_pose = list(pose)  # copy
        idx = posedict_key_to_index["mouth_aaa_index"]
        x = pose[idx]
        x = abs(1.0 - x) + random.uniform(-2.0, 2.0)
        x = max(0.0, min(x, 1.0))  # clamp (not the manga studio)
        new_pose[idx] = x
        return new_pose

    def compute_sway_target_pose(self, original_target_pose: List[float]) -> List[float]:
        """History-free sway animation driver.

        original_target_pose: emotion pose to modify with a randomized sway target

        The target is randomized again when necessary; this takes care of caching internally.

        Return the modified pose.
        """
        # We just modify the target pose, and let the integrator (`interpolate_pose`) do the actual animation.
        # - This way we don't need to track start state, progress, etc.
        # - This also makes the animation nonlinear automatically: a saturating exponential trajectory toward the target.
        #     - If we want to add a smooth start, we'll need a ramp-in mechanism to interpolate the target from the current pose to the actual target gradually.
        #       The nonlinearity automatically takes care of slowing down when the target is approached.

        random_max = 0.6  # max sway magnitude from center position of each morph
        noise_max = 0.02  # amount of dynamic noise (re-generated every frame), added on top of the sway target

        SWAYPARTS = ["head_x_index", "head_y_index", "neck_z_index", "body_y_index", "body_z_index"]

        def macrosway() -> List[float]:  # this handles caching and everything
            time_now = time.time_ns()
            should_pick_new_sway_target = True
            if current_emotion == self.last_emotion:
                if self.sway_interval is not None:  # have we created a swayed pose at least once?
                    seconds_since_last_sway_target = (time_now - self.last_sway_target_timestamp) / 10**9
                    if seconds_since_last_sway_target < self.sway_interval:
                        should_pick_new_sway_target = False
            # else, emotion has changed, invalidating the old sway target, because it is based on the old emotion.

            if not should_pick_new_sway_target:
                if self.last_sway_target_pose is not None:  # When keeping the same sway target, return the cached sway pose if we have one.
                    return self.last_sway_target_pose
                else:  # Should not happen, but let's be robust.
                    return original_target_pose

            new_target_pose = list(original_target_pose)  # copy
            for key in SWAYPARTS:
                idx = posedict_key_to_index[key]
                target_value = original_target_pose[idx]

                # Determine the random range so that the swayed target always stays within `[-random_max, random_max]`, regardless of `target_value`.
                # TODO: This is a simple zeroth-order solution that just cuts the random range.
                #       Would be nicer to *gradually* decrease the available random range on the "outside" as the target value gets further from the origin.
                random_upper = max(0, random_max - target_value)  # e.g. if target_value = 0.2, then random_upper = 0.4  => max possible = 0.6 = random_max
                random_lower = min(0, -random_max - target_value)  # e.g. if target_value = -0.2, then random_lower = -0.4  => min possible = -0.6 = -random_max
                random_value = random.uniform(random_lower, random_upper)

                new_target_pose[idx] = target_value + random_value

            self.last_sway_target_pose = new_target_pose
            self.last_sway_target_timestamp = time_now
            self.sway_interval = random.uniform(5.0, 10.0)  # seconds; duration of this sway target before randomizing new one
            return new_target_pose

        # Add dynamic noise (re-generated every frame) to the target to make the animation look less robotic, especially once we are near the target pose.
        def add_microsway() -> None:  # DANGER: MUTATING FUNCTION
            for key in SWAYPARTS:
                idx = posedict_key_to_index[key]
                x = new_target_pose[idx] + random.uniform(-noise_max, noise_max)
                x = max(-1.0, min(x, 1.0))
                new_target_pose[idx] = x

        new_target_pose = macrosway()
        add_microsway()
        return new_target_pose

    def animate_breathing(self, pose: List[float]) -> List[float]:
        """Breathing animation driver.

        Return the modified pose.
        """
        breathing_cycle_duration = 4.0  # seconds

        time_now = time.time_ns()
        t = (time_now - self.breathing_epoch) / 10**9  # seconds since breathing-epoch
        cycle_pos = t / breathing_cycle_duration  # number of cycles since breathing-epoch
        if cycle_pos > 1.0:  # prevent loss of accuracy in long sessions
            self.breathing_epoch = time_now  # TODO: be more accurate here, should sync to a whole cycle
        cycle_pos = cycle_pos - float(int(cycle_pos))  # fractional part

        new_pose = list(pose)  # copy
        idx = posedict_key_to_index["breathing_index"]
        new_pose[idx] = math.sin(cycle_pos * math.pi)**2  # 0 ... 1 ... 0, smoothly, with slow start and end, fast middle
        return new_pose

    def interpolate_pose(self, pose: List[float], target_pose: List[float], step: float = 0.1) -> List[float]:
        """Rate-based pose integrator. Interpolate from `pose` toward `target_pose`.

        `step`: [0, 1]; how far toward `target_pose` to interpolate. 0 is fully `pose`, 1 is fully `target_pose`.

        Note that looping back the output as `pose`, while keeping `target_pose` constant, causes the current pose
        to approach `target_pose` on a saturating exponential trajectory, like `1 - exp(-lambda * t)`, for some
        constant `lambda`.

        This is because `step` is the fraction of the *current* difference between `pose` and `target_pose`,
        which obviously becomes smaller after each repeat. This is a feature, not a bug!

        This is a kind of history-free rate-based formulation, which needs only the current and target poses, and
        the step size; there is no need to keep track of e.g. the initial pose or the progress along the trajectory.
        """
        # NOTE: This overwrites blinking, talking, and breathing, but that doesn't matter, because we apply this first.
        # The other animation drivers then modify our result.
        new_pose = list(pose)  # copy
        for idx, key in enumerate(posedict_keys):
            # # We now animate blinking *after* interpolating the pose, so when blinking, the eyes close instantly.
            # # This modification would make the blink also end instantly.
            # if key in ["eye_wink_left_index", "eye_wink_right_index"]:
            #     new_pose[idx] = target_pose[idx]
            # else:
            #     ...

            delta = target_pose[idx] - pose[idx]
            new_pose[idx] = pose[idx] + step * delta
        return new_pose

    # --------------------------------------------------------------------------------
    # Animation logic

    def render_animation_frame(self) -> None:
        """Render an animation frame.

        If the previous rendered frame has not been retrieved yet, do nothing.
        """
        if not animation_running:
            return

        # If no one has retrieved the latest rendered frame yet, do not render a new one.
        if self.new_frame_available:
            return

        if global_reload_image is not None:
            self.load_image()
        if self.source_image is None:
            return

        time_render_start = time.time_ns()

        if self.current_pose is None:  # initialize character pose at plugin startup
            self.current_pose = posedict_to_pose(self.emotions[current_emotion])

        emotion_posedict = self.emotions[current_emotion]
        if current_emotion != self.last_emotion:  # some animation drivers need to know when the emotion last changed
            self.last_emotion_change_timestamp = time_render_start

        target_pose = self.apply_emotion_to_pose(emotion_posedict, self.current_pose)
        target_pose = self.compute_sway_target_pose(target_pose)

        self.current_pose = self.interpolate_pose(self.current_pose, target_pose)
        self.current_pose = self.animate_blinking(self.current_pose)
        self.current_pose = self.animate_talking(self.current_pose)
        self.current_pose = self.animate_breathing(self.current_pose)

        # Update this last so that animation drivers have access to the old emotion, too.
        self.last_emotion = current_emotion

        pose = torch.tensor(self.current_pose, device=self.device, dtype=self.poser.get_dtype())

        with torch.no_grad():
            # - [0]: model's output index for the full result image
            # - model's data range is [-1, +1], linear intensity ("gamma encoded")
            output_image = self.poser.pose(self.source_image, pose)[0].float()
            # output_image = (output_image + 1.0) / 2.0  # -> [0, 1]
            output_image.add_(1.0)
            output_image.mul_(0.5)

            self.postprocessor.render_into(output_image)  # apply pixel-space glitch artistry
            output_image = convert_linear_to_srgb(output_image)  # apply gamma correction

            # convert [c, h, w] float -> [h, w, c] uint8
            c, h, w = output_image.shape
            output_image = torch.transpose(output_image.reshape(c, h * w), 0, 1).reshape(h, w, c)
            output_image = (255.0 * output_image).byte()

            output_image_numpy = output_image.detach().cpu().numpy()

        # Update FPS counter, measuring animation frame render time only.
        #
        # This says how fast the renderer *can* run on the current hardware;
        # note we don't actually render more frames than the client consumes.
        time_now = time.time_ns()
        if self.source_image is not None:
            render_elapsed_sec = (time_now - time_render_start) / 10**9
            self.render_duration_statistics.add_datapoint(render_elapsed_sec)

        # Set the new rendered frame as the output image, and mark the frame as ready for consumption.
        with _animator_output_lock:
            self.result_image = output_image_numpy  # atomic replace
            self.new_frame_available = True

        # Log the FPS counter in 5-second intervals.
        if animation_running and (self.last_report_time is None or time_now - self.last_report_time > 5e9):
            avg_render_sec = self.render_duration_statistics.average()
            msec = round(1000 * avg_render_sec, 1)
            fps = round(1 / avg_render_sec, 1) if avg_render_sec > 0.0 else 0.0
            logger.info(f"render: {msec:.1f}ms [{fps} FPS available]")
            self.last_report_time = time_now


class Encoder:
    """Network transport encoder.

    We read each frame from the animator as it becomes ready, and keep it available in `self.image_bytes`
    until the next frame arrives. The `self.image_bytes` buffer is replaced atomically, so this needs no lock
    (you always get the latest available frame at the time you access `image_bytes`).
    """

    def __init__(self) -> None:
        self.image_bytes = None
        self.encoder_thread = None

    def start(self) -> None:
        """Start the output encoder thread."""
        self._terminated = False
        def encoder_update():
            last_report_time = None
            encode_duration_statistics = RunningAverage()
            wait_duration_statistics = RunningAverage()

            while not self._terminated:
                # Retrieve a new frame from the animator if available.
                have_new_frame = False
                time_encode_start = time.time_ns()
                with _animator_output_lock:
                    if global_animator_instance.new_frame_available:
                        image_rgba = global_animator_instance.result_image
                        global_animator_instance.new_frame_available = False  # animation frame consumed; start rendering the next one
                        have_new_frame = True  # This flag is needed so we can release the animator lock as early as possible.

                # If a new frame arrived, pack it for sending (only once for each new frame).
                if have_new_frame:
                    try:
                        pil_image = PIL.Image.fromarray(np.uint8(image_rgba[:, :, :3]))
                        if image_rgba.shape[2] == 4:
                            alpha_channel = image_rgba[:, :, 3]
                            pil_image.putalpha(PIL.Image.fromarray(np.uint8(alpha_channel)))

                        # Save as PNG with RGBA mode. Use the fastest compression level available.
                        #
                        # On an i7-12700H @ 2.3 GHz (laptop optimized for low fan noise):
                        #  - `compress_level=1` (fastest), about 20 ms
                        #  - `compress_level=6` (default), about 40 ms (!) - too slow!
                        #  - `compress_level=9` (smallest size), about 120 ms
                        #
                        # time_now = time.time_ns()
                        buffer = io.BytesIO()
                        pil_image.save(buffer, format="PNG", compress_level=1)
                        image_bytes = buffer.getvalue()
                        # pack_duration_sec = (time.time_ns() - time_now) / 10**9

                        # We now have a new encoded frame; but first, sync with network send.
                        # This prevents from rendering/encoding more frames than are actually sent.
                        previous_frame = self.image_bytes
                        if previous_frame is not None:
                            time_wait_start = time.time_ns()
                            # Wait in 1ms increments until the previous encoded frame has been sent
                            while global_latest_frame_sent != id(previous_frame) and not self._terminated:
                                time.sleep(0.001)
                            time_now = time.time_ns()
                            wait_elapsed_sec = (time_now - time_wait_start) / 10**9
                        else:
                            wait_elapsed_sec = 0.0

                        self.image_bytes = image_bytes  # atomic replace so no need for a lock
                    except Exception as exc:
                        logger.error(exc)
                        raise  # let the encoder stop so we won't spam the log

                    # Update FPS counter.
                    time_now = time.time_ns()
                    walltime_elapsed_sec = (time_now - time_encode_start) / 10**9
                    encode_elapsed_sec = walltime_elapsed_sec - wait_elapsed_sec
                    encode_duration_statistics.add_datapoint(encode_elapsed_sec)
                    wait_duration_statistics.add_datapoint(wait_elapsed_sec)

                # Log the FPS counter in 5-second intervals.
                time_now = time.time_ns()
                if animation_running and (last_report_time is None or time_now - last_report_time > 5e9):
                    avg_encode_sec = encode_duration_statistics.average()
                    msec = round(1000 * avg_encode_sec, 1)
                    avg_wait_sec = wait_duration_statistics.average()
                    wait_msec = round(1000 * avg_wait_sec, 1)
                    fps = round(1 / avg_encode_sec, 1) if avg_encode_sec > 0.0 else 0.0
                    logger.info(f"encode: {msec:.1f}ms [{fps} FPS available]; send sync wait {wait_msec:.1f}ms")
                    last_report_time = time_now

                time.sleep(0.01)  # rate-limit the encoder to 100 FPS maximum (this could be adjusted later)
        self.encoder_thread = threading.Thread(target=encoder_update, daemon=True)
        self.encoder_thread.start()
        atexit.register(self.exit)

    def exit(self) -> None:
        """Terminate the output encoder thread.

        Called automatically when the process exits.
        """
        self._terminated = True
        self.encoder_thread.join()
        self.encoder_thread = None
