# Running the test targets locally

## Swagger Petstore

```
docker run -d --name wfval-petstore -p 127.0.0.1:18080:8080 swaggerapi/petstore3:unstable
workflowval test http://127.0.0.1:18080/api/v3/openapi.json --base-url http://127.0.0.1:18080
```

## Restful-booker

The image is amd64 only, so ARM machines need `--platform linux/amd64`. These credentials are the
practice API's public defaults.

```
docker run -d --platform linux/amd64 --name wfval-booker -p 127.0.0.1:13001:3001 mwinteringham/restfulbooker:latest
export BOOKER_USER=admin BOOKER_PASS=password123
workflowval test examples/restful-booker/openapi.json --base-url http://127.0.0.1:13001 --auth examples/restful-booker/auth.json
```

Choose a model first, with `--model provider/name` or `WORKFLOWVAL_MODEL`.
