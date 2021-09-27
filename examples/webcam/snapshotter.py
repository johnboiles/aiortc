import asyncio
import threading
import io
import logging
import PIL.Image

from av.frame import Frame
from av.packet import Packet

from aiohttp import web

from aiortc.mediastreams import MediaStreamTrack


class Snapshotter:
    def __init__(self, video: MediaStreamTrack) -> None:
        self.thread_quit = asyncio.Event()
        self.thread: threading.Thread = None
        self.jpeg_future: asyncio.Future = asyncio.Future()
        self.last_jpeg: PIL.Image = None
        self.video = video
        self.frame_counter = 0
        self.snapshot_every = 60

    def set_jpeg(self, image: PIL.Image):
        self.last_jpeg = image
        # I'm not totally sure how it happened, but at one point I got into
        # a state where the `jpeg_future` was invalid and this line would crash.
        # We should make sure we can recover by creating a new future if necessary.
        try:
            self.jpeg_future.set_result(image)
        except asyncio.base_futures.InvalidStateError:
            logging.warn(
                "Got InvalidStateError on self.jpeg_future.set_result(image)",
                self.jpeg_future,
            )
        finally:
            self.jpeg_future = asyncio.Future()

    async def get_jpeg(self, await_latest=True) -> PIL.Image:
        if await_latest or self.last_jpeg is None:
            return await self.jpeg_future
        else:
            return self.last_jpeg

    async def snapshot_worker(
        self,
        main_loop: asyncio.BaseEventLoop,
        track: MediaStreamTrack,
        quit: asyncio.Event,
    ):
        while not quit.is_set():
            # Hardware decoding has been less stable
            # Altenately h264_mmal or h264_v4l2m2m
            # import av
            # codec = av.CodecContext.create("h264_mmal", "r")
            # codec.width = 1920
            # codec.height = 1080
            # codec.open()

            data_future = asyncio.run_coroutine_threadsafe(track.recv(), main_loop)
            data = await asyncio.wrap_future(data_future)

            # Retrieve a frame either by decoding I-frame packets or grabbing an
            # already-decoded Frame every `self.snapshot_every` frames
            frame: Frame = None
            if isinstance(data, Frame):
                if self.frame_counter % self.snapshot_every == 0:
                    frame = data
                self.frame_counter += 1
            else:
                packet: Packet = data
                if packet.is_keyframe:
                    frames = packet.decode()
                    frame = frames[0]

            if frame is not None:
                image = frame.to_image()
                main_loop.call_soon_threadsafe(self.set_jpeg, image)
                # Could also write to a file
                # image.save('/run/webcam/jpeg/snapshot.jpg')

    def start(self):
        self.thread = threading.Thread(
            name="snapshotter",
            target=asyncio.run,
            args=(
                self.snapshot_worker(
                    asyncio.get_event_loop(),
                    self.video,
                    self.thread_quit,
                ),
            ),
        )
        self.thread.start()
        return

    def stop(self):
        self.thread_quit.set()

    async def mjpeg_http_route(self, request: web.Request):
        boundary = "jpeg-frame-boundary"
        response = web.StreamResponse(
            status=200,
            reason="OK",
            headers={
                "Content-Type": "multipart/x-mixed-replace;boundary={}".format(boundary)
            },
        )
        await response.prepare(request)
        frames_sent = 0
        while True:
            # Send the first two frames in quick succession so that you get something rendering on first request.
            # For some reason, Chrome and Safari don't render the frame as soon as its sent, but instead only
            # render the previous frame once the next frame is sent.
            # https://bugs.chromium.org/p/chromium/issues/detail?id=527446
            jpeg_frame = await self.get_jpeg(await_latest=(frames_sent > 1))
            img_byte_arr = io.BytesIO()
            jpeg_frame.save(img_byte_arr, format="JPEG")
            if frames_sent == 0:
                await response.write(b"--" + bytes(boundary, "ascii") + b"\r\n")
            await response.write(
                b"Content-Type: image/jpeg\r\nContent-Length: "
                + bytes(str(img_byte_arr.tell()), "ascii")
                + b"\r\n\r\n"
            )
            await response.write(img_byte_arr.getvalue())
            # Firefox will re-render the image as soon as it sees the next boundary, so always providing the next
            # boundary here right after sending the image data will at least make Firefox have better
            # mjpeg latency. Unfortunately this doesn't work for Chrome and Safari :(
            await response.write(b"\r\n\r\n--" + bytes(boundary, "ascii") + b"\r\n")
            frames_sent += 1

    async def jpeg_http_route(self, req: web.Request):
        jpeg_frame = await self.get_jpeg(await_latest=False)
        img_byte_arr = io.BytesIO()
        jpeg_frame.save(img_byte_arr, format="JPEG")
        return web.Response(body=img_byte_arr.getvalue(), content_type="image/jpeg")
