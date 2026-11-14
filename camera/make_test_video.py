"""Builds a synthetic 'glasses camera' video: target, look away, back, a short glance, back."""
import cv2, numpy as np
rng = np.random.default_rng(1)
def scene(seed):
    r = np.random.default_rng(seed); img = np.full((900, 1500, 3), 200, np.uint8)
    for _ in range(260):
        c = tuple(int(x) for x in r.integers(0, 255, 3)); x, y = int(r.integers(0, 1500)), int(r.integers(0, 900))
        if r.random() < .5: cv2.rectangle(img, (x, y), (x + int(r.integers(10, 90)), y + int(r.integers(10, 90))), c, -1)
        else: cv2.circle(img, (x, y), int(r.integers(5, 45)), c, -1)
    return img
room, away = scene(7), scene(99)
fps, out = 15, cv2.VideoWriter('test.mp4', cv2.VideoWriter_fourcc(*'mp4v'), 15, (1280, 720))
plan = [(room, 4), (away, 8), (room, 4), (away, 1.5), (room, 3)]
for img, secs in plan:
    for i in range(int(secs * fps)):
        dx, dy = int(rng.integers(0, 40)), int(rng.integers(0, 40))
        out.write(cv2.resize(img[dy:dy + 720, dx:dx + 1280], (1280, 720)))
out.release(); print('wrote test.mp4')
