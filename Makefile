export AWS_PROFILE ?= personal
export AWS_REGION ?= us-east-2

TF := terraform -chdir=infra

.PHONY: test lint build deploy destroy evals load verify

test:
	uv run pytest -q

lint:
	uv run ruff check .
	uv run ruff format --check .
	$(TF) fmt -check

# Two packages: ingest and worker need only the standard library and the boto3 in the Lambda
# runtime; the api function also bundles anthropic, built for arm64.
build:
	rm -rf build
	mkdir -p build/core build/api
	cp -r app build/core/
	cp -r app build/api/
	uv pip install --quiet --target build/api --python-platform aarch64-manylinux_2_28 \
		--python-version 3.14 --only-binary :all: "anthropic>=1.13,<2"
	find build -name __pycache__ -prune -exec rm -rf {} +

deploy: build
	$(TF) init -input=false
	$(TF) apply

destroy:
	$(TF) destroy

evals:
	uv run python -m evals.run --model $(or $(MODEL),haiku)

load:
	TAILHOOK_SECRET=$$($(TF) output -raw inbound_secret) uv run python -m app.simulator load \
		--url $$($(TF) output -raw webhook_url) --rate $(or $(RATE),1000) --duration $(or $(DURATION),60)

verify:
	uv run python -m app.simulator verify --run $(RUN) \
		--log-group /aws/lambda/$$($(TF) output -raw name)-worker
