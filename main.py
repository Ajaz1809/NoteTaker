from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.middleware.cors import CORSMiddleware  # Import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware  # Import GZip compression

from utils.helpers import format_response
from routes import router
from db.database import engine, Base, note_engine, NoteTakerBase
import os
app = FastAPI()
app.include_router(router)


# Create database tables
# Base.metadata.drop_all(bind=engine)

Base.metadata.create_all(bind=engine)
# Create tables in NoteTaker DB
NoteTakerBase.metadata.create_all(bind=note_engine)


app.mount("/files", StaticFiles(directory="uploads"), name="files")

from starlette.middleware.sessions import SessionMiddleware

app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SESSION_SECRET_KEY", "supersecretlongrandomstring"),
)

# Add response compression middleware (should be added before CORS for best performance)
# Compresses responses larger than 1000 bytes by default
# app.add_middleware(GZipMiddleware, minimum_size=1000)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    errors = []
    for err in exc.errors():
        loc_parts = [str(part) for part in err["loc"] if part != "body"]
        field = ".".join(loc_parts)
        errors.append({"field": field, "message": err["msg"]})

    return JSONResponse(
        status_code=422,
        content=format_response(
            status=False, message="Validation error", errors=errors
        ),
    )


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"status": False, "message": exc.detail, "data": None},
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content=format_response(
            status=False,
            message="Internal server error",
            errors=[{"field": "server", "message": str(exc)}],
        ),
    )


# if __name__ == "__main__":
#     import uvicorn

#     uvicorn.run(app, host="127.0.0.1", port=5003)