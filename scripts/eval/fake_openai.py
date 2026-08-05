"""Cliente de OpenRouter con `urllib`, con la forma que `openai_client` espera.

El `.venv` del proyecto no tiene el SDK de `openai` ni `httpx` (la app corre en
Docker) y no hay pip para instalarlos. Pero la evaluación no puede mockear la
respuesta del modelo: la pregunta es justamente cómo redacta. Así que se
sustituye `openai_client._get_client` por este objeto, que habla con la misma
API por HTTP plano y devuelve la forma mínima que el código consume:

    resp.choices[0].message.content
    resp.choices[0].message.tool_calls[i].id / .function.name / .function.arguments
    resp.choices[0].message.model_dump()

Todo lo demás —construcción del system_prompt, ficha del producto activo,
vocabulario cerrado del clasificador, ronda de tool calls— sigue siendo código
real, y cada árbol que se evalúe aporta sus propios prompts, que es exactamente
lo que se quiere comparar.

Cada petición y su respuesta quedan en un JSONL para poder auditar después de
dónde salió cada veredicto.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path


class _Funcion:
    def __init__(self, datos: dict):
        self.name = datos.get("name")
        self.arguments = datos.get("arguments") or "{}"


class _ToolCall:
    def __init__(self, datos: dict):
        self.id = datos.get("id")
        self.type = datos.get("type", "function")
        self.function = _Funcion(datos.get("function") or {})


class _Mensaje:
    def __init__(self, datos: dict):
        self._crudo = datos
        self.role = datos.get("role", "assistant")
        self.content = datos.get("content")
        llamadas = datos.get("tool_calls") or []
        self.tool_calls = [_ToolCall(t) for t in llamadas] or None

    def model_dump(self) -> dict:
        """`_resolve_model_response` reinyecta el mensaje en la conversación."""
        return dict(self._crudo)


class _Eleccion:
    def __init__(self, datos: dict):
        self.message = _Mensaje(datos.get("message") or {})
        self.finish_reason = datos.get("finish_reason")


class _Respuesta:
    def __init__(self, datos: dict):
        self.choices = [_Eleccion(c) for c in (datos.get("choices") or [{}])]
        self.usage = datos.get("usage")


class ClienteHTTP:
    """Lo mínimo de `AsyncOpenAI` que `openai_client` usa."""

    def __init__(self, api_key: str, base_url: str, registro: Path | None = None,
                 timeout: int = 120, reintentos: int = 3):
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._registro = registro
        self._timeout = timeout
        self._reintentos = reintentos
        self.llamadas = 0
        self.chat = _Chat(self)

    def _post(self, ruta: str, cuerpo: dict) -> dict:
        datos = json.dumps(cuerpo).encode()
        req = urllib.request.Request(
            self._base_url + ruta, data=datos, method="POST",
            headers={"Authorization": f"Bearer {self._api_key}",
                     "Content-Type": "application/json",
                     # OpenRouter pide identificar la aplicación que llama.
                     "HTTP-Referer": "https://tu-deseo.autozb.com",
                     "X-Title": "tu-deseo-bot eval"})
        ultimo_error = None
        for intento in range(self._reintentos):
            try:
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                    return json.load(resp)
            except urllib.error.HTTPError as e:
                detalle = e.read().decode()[:400]
                ultimo_error = RuntimeError(f"HTTP {e.code}: {detalle}")
                # 429 y 5xx merecen reintento; un 4xx de forma, no.
                if e.code != 429 and e.code < 500:
                    raise ultimo_error
            except Exception as e:  # timeouts, cortes de red
                ultimo_error = e
            time.sleep(2 * (intento + 1))
        raise ultimo_error  # type: ignore[misc]

    def crear(self, **kwargs) -> _Respuesta:
        cuerpo = {k: v for k, v in kwargs.items() if k != "extra_body"}
        # El SDK de openai funde `extra_body` en el cuerpo de la petición;
        # aquí hay que hacerlo a mano o se perderían los flags de reasoning.
        cuerpo.update(kwargs.get("extra_body") or {})
        datos = self._post("/chat/completions", cuerpo)
        self.llamadas += 1
        if self._registro:
            with self._registro.open("a") as fh:
                fh.write(json.dumps({"peticion": cuerpo, "respuesta": datos},
                                    ensure_ascii=False) + "\n")
        if datos.get("error"):
            raise RuntimeError(f"OpenRouter devolvió error: {datos['error']}")
        return _Respuesta(datos)


class _Completions:
    def __init__(self, cliente: ClienteHTTP):
        self._cliente = cliente

    async def create(self, **kwargs) -> _Respuesta:
        return self._cliente.crear(**kwargs)


class _Chat:
    def __init__(self, cliente: ClienteHTTP):
        self.completions = _Completions(cliente)


def instalar(modulo_openai_client, env: dict[str, str],
             registro: Path | None = None) -> ClienteHTTP:
    """Deja `openai_client._get_client` devolviendo el cliente HTTP."""
    cliente = ClienteHTTP(
        api_key=env["OPENAI_API_KEY"],
        base_url=env.get("OPENAI_BASE_URL") or "https://api.openai.com/v1",
        registro=registro)
    modulo_openai_client._get_client = lambda: cliente
    return cliente
