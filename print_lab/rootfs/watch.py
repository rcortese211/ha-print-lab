"""Print failure detection for Print Lab.

Grabs one frame from the printer's camera stream with ffmpeg and runs the Obico
("The Spaghetti Detective") failure-detection model on it with onnxruntime on the
CPU. The model is a single-class YOLO detector ("failure"); a frame's score is the
sum of the confidences of its detections, as Obico itself scores frames. Turning
per-frame scores into a decision (smoothing, thresholds, pausing) is left to the
caller.

Model: https://github.com/TheSpaghettiDetective/obico-server (ml_api), AGPL-3.0.
"""
import hashlib
import ipaddress
import os
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

MODEL_URL = "https://www.obico.io/static_pub/ml-models/model-weights-5a6b1be1fa.onnx"
MODEL_SHA256 = "0a6ebd8e30dbf6a450c50f9c0a5406f04ba7eb1c99fd5996e888c78bb383b9aa"
DET_THRESHOLD = 0.2
NMS_THRESHOLD = 0.4
THREADS = 2          # leave the other cores for Home Assistant


def parse_crops(text):
    """'x,y,w,h;x,y,w,h' as fractions of the frame -> [(x, y, w, h), ...]."""
    crops = []
    for part in (text or "").split(";"):
        part = part.strip()
        if not part:
            continue
        x, y, w, h = (float(v) for v in part.split(","))
        if not (0 <= x < 1 and 0 <= y < 1 and 0 < w <= 1 - x + 1e-6 and 0 < h <= 1 - y + 1e-6):
            raise ValueError(f"bad crop {part}")
        crops.append((x, y, w, h))
    return crops


# The print sits in the middle of a wide camera view, small enough that the whole
# frame shrunk to the model's input hides it. Each crop is scored at the model's
# full resolution as well as the whole frame; the frame's score is the highest one.
DEFAULT_CROPS = "0.28,0.2,0.42,0.42;0.28,0.45,0.42,0.42"


class WatchError(Exception):
    pass


class Detector:
    def __init__(self, lab: Path) -> None:
        self.dir = lab / "watch"
        self.history = self.dir / "history"
        self.model_path = lab / "ai" / "obico-failure.onnx"
        self._session = None
        self._lock = threading.Lock()
        self.status = "not loaded"

    # ------------------------------------------------------------------ model
    def ensure_model(self) -> None:
        """Download (once, checksum-verified) and load the model."""
        with self._lock:
            if self._session is not None:
                return
            self.model_path.parent.mkdir(parents=True, exist_ok=True)
            if not self.model_path.is_file() or self._sha(self.model_path) != MODEL_SHA256:
                self.status = "downloading model"
                tmp = self.model_path.with_suffix(".part")
                req = urllib.request.Request(MODEL_URL, headers={"User-Agent": "PrintLab/0.2"})
                with urllib.request.urlopen(req, timeout=300) as r, open(tmp, "wb") as f:
                    shutil.copyfileobj(r, f, 1 << 20)
                if self._sha(tmp) != MODEL_SHA256:
                    tmp.unlink(missing_ok=True)
                    self.status = "model checksum mismatch"
                    raise WatchError("Downloaded model failed its checksum")
                tmp.replace(self.model_path)
            import onnxruntime  # noqa: PLC0415  (only needed once the model is wanted)
            opts = onnxruntime.SessionOptions()
            opts.intra_op_num_threads = THREADS
            self._session = onnxruntime.InferenceSession(
                str(self.model_path), opts, providers=["CPUExecutionProvider"])
            self.status = "ready"

    @staticmethod
    def _sha(path: Path) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    # ------------------------------------------------------------------ frames
    @staticmethod
    def check_stream_url(url: str) -> str:
        p = urllib.parse.urlsplit(url)
        if p.scheme not in ("http", "rtsp") or not p.hostname:
            raise WatchError("Stream must be an http or rtsp address")
        try:
            if not ipaddress.ip_address(p.hostname).is_private:
                raise WatchError("Stream must be on the local network")
        except ValueError:
            pass  # a hostname; HA only ever sends the printer's configured host
        return url

    def grab(self, url: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        args = ["ffmpeg", "-nostdin", "-loglevel", "error", "-y"]
        if url.startswith("http"):
            args += ["-rw_timeout", "10000000"]
        else:
            args += ["-rtsp_transport", "tcp", "-timeout", "10000000"]
        args += ["-i", url, "-frames:v", "1", "-q:v", "3", str(dest)]
        p = subprocess.run(args, capture_output=True, text=True, timeout=40)
        if p.returncode != 0 or not dest.is_file():
            raise WatchError("Could not grab a camera frame: " + (p.stderr.strip()[-300:] or "no output"))

    # ------------------------------------------------------------------ inference
    @staticmethod
    def frame_size(jpg: Path) -> tuple[int, int]:
        p = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=width,height", "-of", "csv=p=0:s=x", str(jpg)],
                           capture_output=True, text=True, timeout=20)
        try:
            w, h = (int(v) for v in p.stdout.strip().split("x")[:2])
        except ValueError as err:
            raise WatchError("Could not read the camera frame size") from err
        return w, h

    def _score_view(self, jpg: Path, vf_crop: str | None) -> tuple[list, float]:
        import numpy as np  # noqa: PLC0415
        sess = self._session
        inp = sess.get_inputs()[0]
        h, w = int(inp.shape[2]), int(inp.shape[3])
        vf = (vf_crop + "," if vf_crop else "") + f"scale={w}:{h}"
        raw = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(jpg),
                              "-vf", vf, "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                             capture_output=True, timeout=30).stdout
        if len(raw) != w * h * 3:
            raise WatchError("Could not decode the camera frame")
        img = np.frombuffer(raw, np.uint8).reshape(h, w, 3).astype(np.float32) / 255.0
        img = np.ascontiguousarray(img.transpose(2, 0, 1)[None])
        started = time.monotonic()
        out = sess.run(None, {inp.name: img})
        took = time.monotonic() - started
        boxes = np.asarray(out[0])[0, :, 0, :]          # [N, 4] x1 y1 x2 y2 (0..1)
        confs = np.asarray(out[1])[0].max(axis=1)       # [N]
        keep = confs > DET_THRESHOLD
        boxes, confs = boxes[keep], confs[keep]
        dets = [([float(v) for v in boxes[i]], float(confs[i])) for i in self._nms(boxes, confs)]
        return dets, took

    def detect(self, jpg: Path, crops=None) -> dict:
        """Score the whole frame and each crop; the frame's score is the best view's."""
        self.ensure_model()
        if crops is None:
            crops = parse_crops(os.environ.get("WATCH_CROPS", DEFAULT_CROPS))
        views = [("full", None, (0.0, 0.0, 1.0, 1.0))]
        for i, (x, y, w, h) in enumerate(crops, 1):
            views.append((f"crop{i}", f"crop=iw*{w}:ih*{h}:iw*{x}:ih*{y}", (x, y, w, h)))
        total_s, per_view, best = 0.0, {}, ("full", 0.0, [])
        for name, vf, (x, y, w, h) in views:
            dets, took = self._score_view(jpg, vf)
            total_s += took
            mapped = [{"conf": round(c, 3), "view": name,
                       "box": [round(x + b[0] * w, 3), round(y + b[1] * h, 3),
                               round(x + b[2] * w, 3), round(y + b[3] * h, 3)]} for b, c in dets]
            score = round(sum(d["conf"] for d in mapped), 3)
            per_view[name] = score
            if score > best[1]:
                best = (name, score, mapped)
        fw, fh = self.frame_size(jpg)
        return {"score": best[1], "view": best[0], "views": per_view, "detections": best[2],
                "inference_s": round(total_s, 2), "frame_size": [fw, fh],
                "crops": [list(c) for c in crops]}

    @staticmethod
    def _nms(boxes, confs):
        import numpy as np  # noqa: PLC0415
        order = confs.argsort()[::-1]
        keep = []
        x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
        areas = (x2 - x1) * (y2 - y1)
        while order.size:
            i = order[0]
            keep.append(int(i))
            xx1 = np.maximum(x1[i], x1[order[1:]])
            yy1 = np.maximum(y1[i], y1[order[1:]])
            xx2 = np.minimum(x2[i], x2[order[1:]])
            yy2 = np.minimum(y2[i], y2[order[1:]])
            inter = np.maximum(0, xx2 - xx1) * np.maximum(0, yy2 - yy1)
            iou = inter / (areas[i] + areas[order[1:]] - inter + 1e-9)
            order = order[1:][iou <= NMS_THRESHOLD]
        return keep

    # ------------------------------------------------------------------ one check
    def check(self, url: str | None = None, jpeg: bytes | None = None, crops=None) -> dict:
        frame = self.dir / "last.jpg"
        if jpeg is not None:
            frame.parent.mkdir(parents=True, exist_ok=True)
            frame.write_bytes(jpeg)
        else:
            self.grab(self.check_stream_url(url), frame)
        result = self.detect(frame, crops)
        result["frame"] = "last.jpg"
        result["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        if result["score"] > 0.3:
            # keep suspicious frames for later tuning
            self.history.mkdir(parents=True, exist_ok=True)
            shutil.copy(frame, self.history / f"{time.strftime('%Y%m%d-%H%M%S')}-{result['score']:.2f}.jpg")
        return result
