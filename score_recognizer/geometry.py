"""四角框與攤正圖片共用座標轉換；原圖永不改寫。"""
from __future__ import annotations

import math
from .contract import ContractError


def rectangle_quad(box):
    x, y, w, h = (box[k] for k in ("x", "y", "width", "height"))
    return [[x, y], [x + w, y], [x + w, y + h], [x, y + h]]


def validate_quad(points):
    if not isinstance(points, list) or len(points) != 4:
        raise ContractError("斜框需要左上、右上、右下、左下四個角")
    for p in points:
        if not isinstance(p, (list, tuple)) or len(p) != 2 or any(
                not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) or not 0 <= v <= 1 for v in p):
            raise ContractError("斜框角點必須在圖片內")
    crosses = []
    for i in range(4):
        a, b, c = points[i], points[(i + 1) % 4], points[(i + 2) % 4]
        crosses.append((b[0]-a[0])*(c[1]-b[1])-(b[1]-a[1])*(c[0]-b[0]))
    if min(crosses) <= .000005:
        raise ContractError("四角不可交叉、重疊或反轉，請按左上、右上、右下、左下排列")
    if points[0][0] >= points[1][0] or points[3][0] >= points[2][0]:
        raise ContractError("斜框左右方向不正確")
    return points


def quad_bounds(points):
    xs, ys = zip(*points)
    return {"x": min(xs), "y": min(ys), "width": max(xs)-min(xs), "height": max(ys)-min(ys)}


def coefficients(points):
    """單位正方形到原圖四角的單應轉換。"""
    validate_quad(points)
    (x0,y0),(x1,y1),(x2,y2),(x3,y3) = points
    dx1, dx2, dx3 = x1-x2, x3-x2, x0-x1+x2-x3
    dy1, dy2, dy3 = y1-y2, y3-y2, y0-y1+y2-y3
    if abs(dx3) + abs(dy3) < 1e-12:
        g = h = 0.0
    else:
        denominator = dx1*dy2-dx2*dy1
        if abs(denominator) < 1e-12:
            raise ContractError("斜框太窄，無法攤正")
        g, h = (dx3*dy2-dx2*dy3)/denominator, (dx1*dy3-dx3*dy1)/denominator
    return x1-x0+g*x1, x3-x0+h*x3, x0, y1-y0+g*y1, y3-y0+h*y3, y0, g, h


def project(points, u, v):
    a,b,c,d,e,f,g,h = coefficients(points)
    den = g*u+h*v+1
    return [(a*u+b*v+c)/den, (d*u+e*v+f)/den]


def region_point(region, u, v):
    return project(region.get("quad") or rectangle_quad(region["bbox"]), u, v)


def region_unit(region, x, y):
    a,b,c,d,e,f,g,h = coefficients(region.get("quad") or rectangle_quad(region["bbox"]))
    aa, bb, dd, ee = a-x*g, b-x*h, d-y*g, e-y*h
    den = aa*ee-bb*dd
    if abs(den) < 1e-12:
        raise ContractError("範圍無法定位")
    return ((x-c)*ee-bb*(y-f))/den, (aa*(y-f)-(x-c)*dd)/den


def source_box(region, box):
    mapped = [region_point(region,u,v) for u,v in rectangle_quad(box)]
    mapped = [[max(0,min(1,x)),max(0,min(1,y))] for x,y in mapped]
    return quad_bounds(mapped)


def rectified_image(image, points):
    from PIL import Image
    validate_quad(points)
    w,h = image.size
    pixels = [(x*w,y*h) for x,y in points]
    length = lambda a,b: math.hypot(a[0]-b[0],a[1]-b[1])
    ow = max(1,round((length(pixels[0],pixels[1])+length(pixels[3],pixels[2]))/2))
    oh = max(1,round((length(pixels[0],pixels[3])+length(pixels[1],pixels[2]))/2))
    a,b,c,d,e,f,g,k = coefficients(points)
    transform = (a*w/ow,b*w/oh,c*w,d*h/ow,e*h/oh,f*h,g/ow,k/oh)
    return image.transform((ow,oh),Image.Transform.PERSPECTIVE,transform,Image.Resampling.BICUBIC,fillcolor="white")
