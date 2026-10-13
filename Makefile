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

# One package for every function: the standard library and the boto3 in the Lambda runtime.
build:
	rm -rf build
	mkdir -p build/core
	cp -r app build/core/
	find build -name __pycache__ -prune -exec rm -rf {} +

deploy: build
	$(TF) init -input=false
	$(TF) apply

destroy:
	$(TF) destroy

evals:
	uv run python -m evals.run --model $(or $(MODEL),gpt-oss)

load:
	TAILHOOK_SECRET=$$($(TF) output -raw inbound_secret) uv run python -m app.simulator load \
		--url $$($(TF) output -raw webhook_url) --rate $(or $(RATE),1000) --duration $(or $(DURATION),60)

verify:
	uv run python -m app.simulator verify --run $(RUN) \
		--log-group /aws/lambda/$$($(TF) output -raw name)-worker
