"""OMR 專用淡線處理；原始裁圖與音符位置不變。"""
from pathlib import Path
from statistics import median
import xml.etree.ElementTree as ET
import zipfile

from PIL import ImageDraw,ImageOps


def enhance(image):
    return ImageOps.autocontrast(ImageOps.grayscale(image),cutoff=.5)


def strengthen_connection(image, book):
    """只在原圖確有跨聲部淡直線時加深，不能憑空連接分開的譜行。"""
    try:
        with zipfile.ZipFile(book) as archive:
            entry=archive.getinfo('sheet#1/sheet#1.xml')
            if entry.file_size>50_000_000:
                return None
            root=ET.fromstring(archive.read(entry))
        if len(list(root.iter('system'))) < 2:
            return None
        staffs=[]
        for staff in root.iter('staff'):
            lines=staff.findall('lines/line')
            if len(lines)!=5:
                return None
            points=[line.find('point') for line in lines]
            if any(p is None for p in points):
                return None
            ys=sorted(float(p.get('y')) for p in points)
            staffs.append((float(staff.get('left')),float(staff.get('right')),ys[0],ys[-1]))
        if not 2<=len(staffs)<=32:
            return None
        staffs.sort(key=lambda s:s[2])
        interline=median((s[3]-s[2])/4 for s in staffs)
        if interline<=0 or max(s[0] for s in staffs)-min(s[0] for s in staffs)>interline:
            return None
        if max(s[1] for s in staffs)-min(s[1] for s in staffs)>image.width*.04:
            return None
        if min(s[1]-s[0] for s in staffs)<image.width*.6:
            return None
        gaps=[(round(a[3]+interline*.4),round(b[2]-interline*.4)) for a,b in zip(staffs,staffs[1:])]
        lengths=[end-start for start,end in gaps]
        if min(lengths)<interline or max(lengths)>median(lengths)*2.5:
            return None
        gray=ImageOps.grayscale(image)
        candidates=[]
        left=round(min(s[0] for s in staffs)-interline)
        right=round(max(s[0] for s in staffs)+interline*.25)
        for x in range(max(0,left),min(image.width,right+1)):
            coverage=[]
            for start,end in gaps:
                pixels=[gray.getpixel((x,y)) for y in range(max(0,start),min(gray.height,end))]
                if not pixels:
                    return None
                coverage.append(sum(p<220 for p in pixels)/len(pixels))
            if min(coverage)>=.55:
                candidates.append((sum(coverage)/len(coverage),x))
        if not candidates:
            return None
        best=max(score for score,x in candidates)
        x=round(median(x for score,x in candidates if score>=best-.03))
        width=max(1,round(interline/5))
        result=image.copy()
        ImageDraw.Draw(result).line((x,round(staffs[0][2]),x,round(staffs[-1][3])),fill=0,width=width)
        return result,{"kind":"strengthen_existing_staff_connection","x":x,"width":width,"staff_count":len(staffs),"minimum_gap_ink_fraction":round(best,3)}
    except (OSError,ValueError,KeyError,TypeError,ET.ParseError,zipfile.BadZipFile):
        return None
