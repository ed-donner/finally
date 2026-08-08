from contextlib import asynccontextmanager

from dotenv import load_dotenv

load_dotenv()  # populate os.environ from .env BEFORE importing config/settings

from fastapi import FastAPI  # noqa: E402

from db.reads import load_active_tickers  # noqa: E402
from market.cache import PriceCache  # noqa: E402
from market.factory import make_source  # noqa: E402
from market.feed import MarketFeed  # noqa: E402
from market.routes import router as market_router  # noqa: E402


@asynccontextmanager
async def lifespan(app: FastAPI):
    cache = PriceCache()
    source = make_source()
    feed = MarketFeed(source, cache, get_active_tickers=load_active_tickers)

    app.state.price_cache = cache
    app.state.market_source = source
    app.state.market_feed = feed

    await feed.prime()   # one synchronous fill so the first request has data
    feed.start()         # then the background loop takes over
    try:
        yield
    finally:
        await feed.stop()


app = FastAPI(lifespan=lifespan)
app.include_router(market_router)


@app.get("/api/health")
async def health():
    return {"status": "ok"}
