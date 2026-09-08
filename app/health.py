async def start_health_server(port):
    async def handler(reader, writer):
        try:
            await reader.read(1024)
            body = b'{"status":"ok"}'
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 15\r\n\r\n" + body)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
    import asyncio
    return await asyncio.start_server(handler, "0.0.0.0", port)
