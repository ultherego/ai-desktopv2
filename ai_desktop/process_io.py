"""Bounded process pipes; caller must provide a PID namespace for descendants."""
import os
import selectors
import signal
import subprocess
import time

from .analysis_contract import AnalysisDenied, MAX_PACKET_LARGE


MAX_INPUT_BYTES = MAX_PACKET_LARGE
MAX_PRIVATE_INPUT_BYTES = MAX_INPUT_BYTES + 17 * 1024


def bounded_process(argv, *, env, input_bytes=b"", timeout=120, limit=131072, cancelled=None,
                    input_limit=32768, private_input=False, copy_input=False, stdout_chunk=None,
                    pass_fds=()):
    ceiling = MAX_PRIVATE_INPUT_BYTES if private_input is True else MAX_INPUT_BYTES
    if copy_input is True:
        from .copy_contract import MAX_PAYLOAD
        ceiling = MAX_PAYLOAD
    if (type(pass_fds) is not tuple or len(pass_fds) > 4096
            or any(type(fd) is not int or fd < 3 for fd in pass_fds)
            or len(set(pass_fds)) != len(pass_fds)
            or type(private_input) is not bool or type(copy_input) is not bool or (private_input and copy_input)
            or type(input_limit) is not int or not 0 <= input_limit <= ceiling
            or type(input_bytes) is not bytes or len(input_bytes) > input_limit):
        raise AnalysisDenied("input")
    try:
        for descriptor in pass_fds:
            os.fstat(descriptor)
    except OSError:
        raise AnalysisDenied("input") from None
    if cancelled is not None and cancelled():
        raise AnalysisDenied("cancelled")
    process = subprocess.Popen(argv, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, close_fds=True, start_new_session=True,
                               pass_fds=pass_fds)
    output, offset = [bytearray(), bytearray()], 0
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            for stream, index in ((process.stdout, 0), (process.stderr, 1)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, index)
            if input_bytes:
                os.set_blocking(process.stdin.fileno(), False)
                selector.register(process.stdin, selectors.EVENT_WRITE, 2)
            else:
                process.stdin.close()
            while selector.get_map():
                if cancelled is not None and cancelled():
                    raise AnalysisDenied("cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AnalysisDenied("timeout")
                for key, _ in selector.select(min(remaining, .2)):
                    if key.data == 2:
                        try:
                            offset += os.write(key.fileobj.fileno(), input_bytes[offset:offset + 8192])
                        except BlockingIOError:
                            continue
                        if offset == len(input_bytes):
                            selector.unregister(key.fileobj)
                            process.stdin.close()
                    else:
                        try:
                            block = os.read(key.fileobj.fileno(), 8192)
                        except BlockingIOError:
                            continue
                        if not block:
                            selector.unregister(key.fileobj)
                        else:
                            output[key.data].extend(block)
                            if sum(map(len, output)) > limit:
                                raise AnalysisDenied("output-limit")
                            if key.data == 0 and stdout_chunk is not None:
                                stdout_chunk(block)
            while process.poll() is None:
                if cancelled is not None and cancelled():
                    raise AnalysisDenied("cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AnalysisDenied("timeout")
                try:
                    process.wait(timeout=min(.2, remaining))
                except subprocess.TimeoutExpired:
                    pass
        return process.returncode, bytes(output[0]), bytes(output[1])
    except subprocess.TimeoutExpired:
        raise AnalysisDenied("timeout") from None
    except OSError:
        raise AnalysisDenied("client") from None
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()
