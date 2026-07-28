import os


class Settings:
    # --- Trino / Presto connection ---
    PRESTO_HOST: str = os.getenv("PRESTO_HOST", "79c4dc32-f80d-4cbe-b088-a894215b7cce.d4mn509d0ncc5tmbn6sg.lakehouse.ibmappdomain.cloud")
    PRESTO_PORT: int = int(os.getenv("PRESTO_PORT", "31351"))
    PRESTO_USER: str = os.getenv("PRESTO_USER", "ibmlhapikey_utkarshj@gadieltechnologies.com")
    PRESTO_CATALOG: str = os.getenv("PRESTO_CATALOG", "purchase")
    PRESTO_SCHEMA: str = os.getenv("PRESTO_SCHEMA", "procurement_view")
    PRESTO_TABLE: str = os.getenv("PRESTO_TABLE", "purchase_unified_testing")
    PRESTO_PASSWORD: str = os.getenv("PRESTO_PASS", "kEYC-iaRZRuEb0AIck5x1iCDB32Zdb8MkC_3j6AzpIz3")

    # --- LLM (Anthropic) for intent/entity extraction ---
    WX_URL: str = os.getenv("WX_URL", "https://us-south.ml.cloud.ibm.com")
    LLM_MODEL_ID: str = os.getenv("WX_MODEL_ID", "openai/gpt-oss-120b")
    WX_API_KEY: str = os.getenv("WX_API_KEY", "G81fXTF1OyESq-WViDqYzg-d8sM1SWJizq7UcneWGHGc")  
    WX_PROJECT_ID: str = os.getenv("WX_PROJECT_ID", "4152f31e-6a49-40aa-9b62-0ecf629aae42")
    WX_TEMPERATURE: float = float(os.getenv("WX_TEMPERATURE", "0.4"))
    WX_MAX_TOKENS: int = int(os.getenv("WX_MAX_TOKENS", "3000"))

    # --- App behavior ---
    MAX_ROWS: int = int(os.getenv("MAX_ROWS", "500"))
    QUERY_TIMEOUT_SECONDS: int = int(os.getenv("QUERY_TIMEOUT_SECONDS", "30"))


settings = Settings()