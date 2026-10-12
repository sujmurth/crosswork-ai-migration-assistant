# srv6-migration-agent — build, deploy, and run the CWAI ADK agent.
#
# Prerequisites:
#   - Docker
#   - cwaictl authenticated (`cwaictl auth login`)
#   - CWAI_BASE_URL set (platform API base URL, e.g. https://172.20.163.87)
#
# Variables (override on the command line):
#   IMAGE_NAME  Docker image name (default: srv6-migration-agent)
#   IMAGE_TAG   Image tag (default: 1.0.0)

IMAGE_NAME = srv6-migration-agent
IMAGE_TAG = 1.0.0
PYTHON_SRCS = main.py

.PHONY: all build install uninstall run clean loggedin

all: build

build:
	python3 -m py_compile $(PYTHON_SRCS) || ( echo "\n\nPython sources do not compile, please fix the errors and try again\n\n" && exit 1 )

	docker build -f Dockerfile -t $(IMAGE_NAME):$(IMAGE_TAG) -t apps/cisco-crosswork-ai/$(IMAGE_NAME):$(IMAGE_TAG) .
	docker save $(IMAGE_NAME):$(IMAGE_TAG) > image.tar

	# Update metadata.json with the image tag and version
	( rm metadata.json && jq --arg tag "$(IMAGE_TAG)" '.version = $$tag' > metadata.json ) < metadata.json

	tar -cf $(IMAGE_NAME).tar image.tar metadata.json

install: build loggedin
	cwaictl agent delete --name $(IMAGE_NAME) || true
	curl -k -X POST "${CWAI_BASE_URL}/api/v1/agents" \
		-H "Cookie: AuthCookie=$$(jq -r .token ~/.cwaictl/token.json)" \
		-F "tags=$(IMAGE_NAME)" \
		-F "archive=@$(IMAGE_NAME).tar"

uninstall: loggedin
	cwaictl agent delete --name $(IMAGE_NAME)

run: loggedin
	cwaictl agent run --name $(IMAGE_NAME) --request '{"prompt": "What tools are available?"}'

clean:
	rm -rf $(IMAGE_NAME).tar image.tar __pycache__

loggedin:
	cwaictl agent list >/dev/null || cwaictl auth login --username admin
	jq -r .token ~/.cwaictl/token.json > /dev/null
