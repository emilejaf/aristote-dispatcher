from aiohttp import web


PROBES_PORT = 8081


class Prober:
    def __init__(self):
        self.startup = False
        self.app = web.Application()
        self.app.router.add_get("/health", self.handle_health_check)
        self.app.router.add_get("/startup", self.handle_startup_check)
        self.runner = web.AppRunner(self.app)
        self.site = None

    async def setup(self):
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "0.0.0.0", PROBES_PORT)
        await self.site.start()

    async def cleanup(self):
        await self.site.stop()
        await self.runner.cleanup()

    async def handle_health_check(self, request):
        # Consumer is self-healing, it is unhealthy only if it has crashed
        # It will then stop answering to health checks
        return web.Response(text="OK", status=200)
        
    async def set_started(self):
        self.startup = True

    async def handle_startup_check(self, request):
        if self.startup:
            return web.Response(text="Started", status=200)
        else:
            return web.Response(text="Not started", status=500)


prober = Prober()