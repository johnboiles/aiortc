import argparse
import asyncio
import json
import logging
import os
import ssl
import threading
import io
import av

from aiohttp import web, MultipartWriter

from aiortc.contrib.media import MediaPlayer

ROOT = os.path.dirname(__file__)

thread_quit = threading.Event()
thread: threading.Thread = None
jpeg_future: asyncio.Future = None


async def snapshot_worker(main_loop: asyncio.BaseEventLoop, quit: threading.Event):
    player = MediaPlayer(
        "/dev/video0",
        format="v4l2",
        transcode=args.transcode,
        options=args.video_options,
    )

    while not quit.is_set():

        # Hardware decoding has been less stable
        # Altenately h264_mmal or h264_v4l2m2m
        # codec = av.CodecContext.create("h264_mmal", "r")
        # codec.width = 1920
        # codec.height = 1080
        # codec.open()

        # This requires a hack to not packetize in media.py:263 so that it sends back packets
        # and not just bytes
        packet = await player.video.recv_packet()
        if packet.is_keyframe:
            print(packet)
            frames = packet.decode()
            print(frames)
            # for frame in frames:
            global jpeg_future
            image = frames[0].to_image()
            # Could also write to a file
            # image.save('/run/webcam/jpeg/snapshot.jpg')
            main_loop.call_soon_threadsafe(jpeg_future.set_result, image)
            jpeg_future = asyncio.Future(loop=main_loop)


async def index(request):
    content = open(os.path.join(ROOT, "index.html"), "r").read()
    return web.Response(content_type="text/html", text=content)


async def mjpeg(request):
    my_boundary = "jpeg-frame-boundary"
    response = web.StreamResponse(
        status=200,
        reason="OK",
        headers={
            "Content-Type": "multipart/x-mixed-replace;boundary={}".format(my_boundary)
        },
    )
    await response.prepare(request)
    while True:
        jpeg_frame = await asyncio.shield(jpeg_future)
        with MultipartWriter("image/jpeg", boundary=my_boundary) as mpwriter:
            img_byte_arr = io.BytesIO()
            jpeg_frame.save(img_byte_arr, format="JPEG")
            mpwriter.append(img_byte_arr.getvalue(), {"Content-Type": "image/jpeg"})
            await mpwriter.write(response, close_boundary=False)
        # This line logs:
        #   DeprecationWarning: drain method is deprecated, use await resp.write()
        # If I remove this it seems that the first frame is incomplete however.
        # TODO: figure out the blessed approach here
        await response.drain()


async def on_shutdown(app):
    print("on_shutdown")
    global thread_quit
    global thread
    thread_quit.set()
    thread.join()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="PyAV MJPEG from raw h264 example")
    parser.add_argument("--cert-file", help="SSL certificate file (for HTTPS)")
    parser.add_argument("--key-file", help="SSL key file (for HTTPS)")
    parser.add_argument(
        "--host", default="0.0.0.0", help="Host for HTTP server (default: 0.0.0.0)"
    )
    parser.add_argument(
        "--port", type=int, default=8080, help="Port for HTTP server (default: 8080)"
    )
    parser.add_argument("--verbose", "-v", action="count")
    parser.add_argument(
        "--video-options", type=json.loads, help="Options to pass into av.open"
    )

    transcode_parser = parser.add_mutually_exclusive_group(required=False)
    transcode_parser.add_argument("--transcode", dest="transcode", action="store_true")
    transcode_parser.add_argument(
        "--no-transcode", dest="transcode", action="store_false"
    )
    parser.set_defaults(transcode=True)

    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

    if args.cert_file:
        ssl_context = ssl.SSLContext()
        ssl_context.load_cert_chain(args.cert_file, args.key_file)
    else:
        ssl_context = None

    jpeg_future = asyncio.Future()
    thread = threading.Thread(
        name="snapshotter",
        target=asyncio.run,
        args=(
            snapshot_worker(
                asyncio.get_event_loop(),
                thread_quit,
            ),
        ),
    )
    thread.start()

    app = web.Application()
    app.on_shutdown.append(on_shutdown)
    app.router.add_get("/", index)
    app.router.add_get("/mjpeg", mjpeg)
    web.run_app(app, host=args.host, port=args.port, ssl_context=ssl_context)
