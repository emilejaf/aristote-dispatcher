import time
import logging
import time
import json
from httpx import AsyncClient
from aio_pika import Message
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse, PlainTextResponse
from starlette.background import BackgroundTask, BackgroundTasks
from contextlib import asynccontextmanager
from db import Database
from rpc_client import RPCClient
from settings import Settings
from models import get_model_by_id, get_models

settings = Settings()
logging.basicConfig(
    level=settings.LOG_LEVEL, format="%(asctime)s:%(levelname)s:%(name)s: %(message)s"
)

database = None
rpc_client = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.info("Opening database connection")
    global database
    database = await Database.init_database(settings=settings)
    logging.info("Database connection opened")

    logging.info("Opening RPC connection")
    global rpc_client
    rpc_client = RPCClient(settings=settings)
    await rpc_client.connect()
    logging.info("RPC connection opened")

    yield

    await database.close()
    await rpc_client.close()


app = FastAPI(lifespan=lifespan)


models = {"object": "list", "data": []}  # object return by endpoint /v1/models


async def authorize(request: Request):

    authorization = request.headers.get("Authorization")

    if not authorization:
        raise Exception("No token provided")

    token_parts = authorization.split()

    if len(token_parts) != 2 or token_parts[0] != "Bearer":
        raise Exception("Invalid token")

    user = await database.execute(
        "SELECT * FROM users WHERE token = %s",
        "SELECT * FROM users WHERE token = $1",
        token_parts[1],
    )

    if not user:
        raise Exception("Unauthorized")

    return user


@app.get("/v1/models")
async def models():
    return JSONResponse(content=await get_models(settings), status_code=200)


@app.middleware("http")
async def proxy(request: Request, call_next):
    start = time.localtime()
    start_hour = f"{start.tm_hour}:{start.tm_min}:{start.tm_sec}"
    logging.info(f"Received request on path {request.url.path}")

    if request.method == "GET" and request.url.path == "/health/startup":
        return PlainTextResponse(content="OK", status_code=200)
    
    if request.method == "GET" and request.url.path == "/health/liveness":
        return PlainTextResponse(content="OK", status_code=200)
    
    # Readiness check is useful as sender can be loadbalanced
    if request.method == "GET" and request.url.path == "/health/readiness":
        state = await rpc_client.check_connection()
        if state:
            return PlainTextResponse(content="OK", status_code=200)
        else:
            return PlainTextResponse(content="KO", status_code=503)

    # Authorization
    try:
        user = await authorize(request)
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=401)
    user_id, token, priority, threshold = user
    threshold = 0 if threshold is None else threshold

    logging.info("User fetched")

    # Handle GET request normally
    if request.method == "GET":
        return await call_next(request)

    body = await request.body()
    try:
        json_body = json.loads(body)
    except json.decoder.JSONDecodeError:
        return JSONResponse(content={"error": "Invalid JSON Body"}, status_code=400)

    # Handle request without model in the body
    if not json_body["model"]:
        return JSONResponse(
            content={
                "object": "error",
                "message": "No model specified.",
                "type": "NotFoundError",
            },
            status_code=404,
        )

    requested_model = json_body["model"]

    if not await get_model_by_id(settings, requested_model):
        return JSONResponse(
            content={
                "object": "error",
                "error": "Unknown model",
            },
            status_code=404,
        )

    rpc_response = await rpc_client.call(priority, threshold, requested_model)

    if type(rpc_response) == int:
        response_content = {"error": "Too many people using the service"}
        # TODO: Don't distinguish users by stream or not, use human / machine instead
        if json_body["stream"]:
            # in case of stream, we set status code to 200 to avoid OpenWebUI to crash
            return PlainTextResponse(content=response_content, status_code=200)
        else:
            return JSONResponse(content=response_content, status_code=503)
    logging.info("rpc response received")

    llm_url = rpc_response.body.decode()
    logging.info(f"LLM Url received : {llm_url}")

    http_client = AsyncClient(base_url=llm_url, timeout=300.0)
    req = http_client.build_request(
        method=request.method, url=request.url.path, content=body
    )

    logging.info(
        f"request: \nmethod: {request.method}\nurl: {request.url.path}\ncontent: {body}"
    )
    logging.info("async proxy request created")

    try:
        res = await http_client.send(req, stream=True)
        logging.info("async proxy request send")
        background_tasks = BackgroundTasks(
            [
                BackgroundTask(res.aclose),
                BackgroundTask(http_client.aclose),
                BackgroundTask(
                    logging.info, f"Fin de la requête lancée à {start_hour}"
                ),
            ]
        )
    except Exception as e:
        logging.error(e)
        return JSONResponse(content={"error": "Internal error"}, status_code=500)
    else:
        return StreamingResponse(
            res.aiter_raw(), headers=res.headers, background=background_tasks
        )