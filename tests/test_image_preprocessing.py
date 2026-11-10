"""Image preprocessing geometry, original preservation and stream ownership."""
from contextlib import ExitStack

import pytest
from PIL import Image
from image_preprocessing import prepare_image_upload


def stripes(path, vertical=False):
    size = (100,300) if vertical else (300,100)
    im = Image.new('RGB',size)
    for i,color in enumerate(('red','green','blue')):
        box = (0,i*100,100,(i+1)*100) if vertical else (i*100,0,(i+1)*100,100)
        im.paste(color,box)
    im.save(path)
    im.close()


def test_original_is_byte_identical_and_closes(tmp_path):
    path = tmp_path/'original.png'
    path.write_bytes(b'original-bytes-with-metadata')
    with ExitStack() as stack:
        name,stream,mime = prepare_image_upload(path,'original',None,None,stack)
        assert stream.read() == path.read_bytes()
        assert name == path.name and mime == 'image/png'
    assert stream.closed


@pytest.mark.parametrize('vertical',[False,True])
def test_crop_is_centered_and_preserves_original(tmp_path,vertical):
    path = tmp_path/'wide.png'
    stripes(path,vertical)
    original = path.read_bytes()
    with ExitStack() as stack:
        name,stream,mime = prepare_image_upload(path,'crop',100,100,stack)
        with Image.open(stream) as result:
            assert result.size == (100,100)
            assert result.getpixel((0,0)) == (0,128,0)
            assert result.getpixel((99,99)) == (0,128,0)
        assert name.endswith('_crop.png') and mime == 'image/png'
    assert stream.closed and path.read_bytes() == original


def test_resize_keeps_entire_image_in_target_canvas(tmp_path):
    path = tmp_path/'wide.png'
    stripes(path)
    with ExitStack() as stack:
        _,stream,_ = prepare_image_upload(path,'resize',100,100,stack)
        with Image.open(stream) as result:
            assert result.size == (100,100)
            assert result.getpixel((2,50)) == (255,0,0)
            assert result.getpixel((50,50)) == (0,128,0)
            assert result.getpixel((97,50)) == (0,0,255)


def test_preprocessing_honors_exif_orientation(tmp_path):
    path = tmp_path/'rotated.jpg'
    im = Image.new('RGB',(80,40),'red')
    im.paste('blue',(40,0,80,40))
    exif=im.getexif();exif[274]=6
    im.save(path,exif=exif)
    with ExitStack() as stack:
        _,stream,_ = prepare_image_upload(path,'resize',20,40,stack)
        with Image.open(stream) as result:
            assert result.getpixel((10,5))[0]>200
            assert result.getpixel((10,35))[2]>200
            assert result.getexif().get(274) is None


@pytest.mark.parametrize('mode,width,height',[('invalid',100,100),('resize',0,100),('crop',100,None)])
def test_invalid_preprocessing_rejected(tmp_path,mode,width,height):
    with ExitStack() as stack, pytest.raises(ValueError):
        prepare_image_upload(tmp_path/'x.png',mode,width,height,stack)


def test_manual_crop_selects_off_center_region(tmp_path):
    path = tmp_path / "stripes.png"
    stripes(path)
    with ExitStack() as stack:
        _, stream, _ = prepare_image_upload(path, "crop", 48, 96, stack,
            crop={"x": 2/3, "y": 0, "width": 1/3, "height": 1})
        with Image.open(stream) as result:
            assert result.size == (48, 96)
            assert result.getpixel((24, 48)) == (0, 0, 255)


@pytest.mark.parametrize("crop", [None, {}, {"x": -1, "y": 0, "width": 1, "height": 1},
    {"x": 0, "y": 0, "width": float("nan"), "height": 1},
    {"x": 0.5, "y": 0, "width": 1, "height": 1}])
def test_invalid_crop_rejected(crop):
    from image_preprocessing import validate_crop
    with pytest.raises(ValueError):
        validate_crop(crop)
