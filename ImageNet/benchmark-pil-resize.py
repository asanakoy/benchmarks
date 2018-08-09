#!/usr/bin/env python
# -*- coding: utf-8 -*-
# File: benchmark-opencv-resize.py


from PIL import Image
import time
import numpy as np

"""
Some prebuilt opencv is much slower than others.
You should check with this script and make sure it prints < 1s.

"""


img_np = (np.random.rand(256, 256, 3) * 255).astype('uint8')
start = time.time()
for k in range(1000):
    img = Image.fromarray(img_np)
    out = img.resize((384, 384))
print(time.time() - start)
