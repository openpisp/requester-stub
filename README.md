# OpenPISP Requester Stub

A merchant terminal simulator for testing PISP integrations.

Simulates a payment requester (merchant/terminal) interacting with a PISP:
creating payment requests, polling for status, receiving webhooks, and
managing QR tokens. Useful for automated testing and demos without a
real merchant integration.

## Quick start

```bash
docker run -p 8000:8000 \
  -e PISP_URL=https://finova.prisac.com \
  -e TERMINAL_API_KEY=sk-your-key \
  ghcr.io/openpisp/requester-stub:latest
```

## Configuration

| Variable | Default | Description |
|---|---|---|
| `PISP_URL` | `http://localhost:8001` | PISP base URL |
| `TERMINAL_API_KEY` | `` | Terminal API key issued by the PISP |
| `CALLBACK_URL` | `` | Webhook callback URL for payment status events |

## Part of the OpenPISP Reference Implementation

See [openpisp/reference-implementation](https://github.com/openpisp/reference-implementation).
