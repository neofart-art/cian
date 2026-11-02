"""Build a readable preview from synthetic CI screenshots; no application data."""
import base64
import os
from pathlib import Path
from PIL import Image,ImageDraw,ImageOps

root=Path(os.getenv('UI_SCREENSHOT_DIR','artifacts/ui-screenshots'))
names=['chromium-1440-all','chromium-1440-ours','chromium-390-all','chromium-390-ours','chromium-390-comment-editor']
if not all((root/(name+'.png')).exists() for name in names):
    print('Screenshot preview not yet complete')
else:
    canvas=Image.new('RGB',(1440,1640),'#f4f3ec')
    draw=ImageDraw.Draw(canvas)
    locations=[(0,0,720,700),(720,0,720,700),(0,720,460,900),(480,720,460,900),(960,720,460,900)]
    for name,(x,y,w,h) in zip(names,locations):
        with Image.open(root/(name+'.png')) as source:
            # Keep text readable: crop to the top viewport rather than
            # shrinking an entire long mobile page into a tiny thumbnail.
            crop=source.crop((0,0,source.width,min(source.height,1100 if source.width>1000 else 900)))
            preview=ImageOps.contain(crop,(w,h-26))
            canvas.paste(preview,(x,y+26))
        draw.text((x+8,y+6),name,fill='#263d37')
    target=root/'preview.png';canvas.save(target,optimize=True)
    print('XATA_SCREENSHOT_PREVIEW:'+base64.b64encode(target.read_bytes()).decode())
