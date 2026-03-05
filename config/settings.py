import os
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

client = OpenAI()

# CONFIGURATION
SECRET_KEY = os.getenv("JWT_SECRET", "supersecret")
ALGORITHM = "HS256"
DATABASE_URL = os.getenv("DATABASE_URL")
NOTETAKER_DATABASE_URL = os.getenv("NOTETAKER_DATABASE_URL")
CONFERENCE_DATABASE_URL = os.getenv("CONFERENCE_DATABASE_URL")