from pydantic import BaseModel


class PublicKeyOut(BaseModel):
    kid: str
    alg: str
    jwk: dict[str, str]


class SessionIn(BaseModel):
    kid: str
    wrappedKey: str


class SessionOut(BaseModel):
    sessionId: str
    expiresInSeconds: int
