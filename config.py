from dotenv import load_dotenv
import os
load_dotenv()  # Load environment variables from .env file

class Settings:
    # --- Trino / Presto connection ---
    PRESTO_HOST: str = os.getenv("PRESTO_HOST")
    PRESTO_PORT: int = int(os.getenv("PRESTO_PORT"))
    PRESTO_USER: str = os.getenv("PRESTO_USER")
    PRESTO_CATALOG: str = os.getenv("PRESTO_CATALOG")
    PRESTO_SCHEMA: str = os.getenv("PRESTO_SCHEMA")
    PRESTO_TABLE: str = os.getenv("PRESTO_TABLE")
    PRESTO_PASSWORD: str = os.getenv("PRESTO_PASSWORD")

    # --- LLM (Anthropic) for intent/entity extraction ---
    WX_URL: str = os.getenv("WX_URL",)
    WX_MODEL_ID: str = os.getenv("WX_MODEL_ID",)
    WX_API_KEY: str = os.getenv("WX_API_KEY",)  
    WX_PROJECT_ID: str = os.getenv("WX_PROJECT_ID",)
    WX_TEMPERATURE: float = float(os.getenv("WX_TEMPERATURE", "0.4"))
    WX_MAX_TOKENS: int = int(os.getenv("WX_MAX_TOKENS", "3000"))
    WX_IAM_TOKEN_URL: str = os.getenv("WX_IAM_TOKEN_URL",)

    # --- App behavior ---
    MAX_ROWS: int = int(os.getenv("MAX_ROWS", "500"))
    # Trend questions ("month over month how many PRs were created") that name no
    # period default to the current financial year rather than scanning all history.
    # Only trends are defaulted -- point lookups like "details for PO 5400010477"
    # must stay unfiltered.
    TREND_DEFAULT_TO_CURRENT_FY: bool = os.getenv("TREND_DEFAULT_TO_CURRENT_FY", "true").lower() == "true"
    # Year-grain trends are the exception: one FY is a single bucket, so a year-over-year
    # question inside the current FY has nothing to compare against. Default those to the
    # last N financial years instead. Set to 1 for strict current-FY-only behaviour.
    TREND_DEFAULT_FY_SPAN_YEAR_GRAIN: int = int(os.getenv("TREND_DEFAULT_FY_SPAN_YEAR_GRAIN", "3"))
    QUERY_TIMEOUT_SECONDS: int = int(os.getenv("QUERY_TIMEOUT_SECONDS", "30"))



settings = Settings()