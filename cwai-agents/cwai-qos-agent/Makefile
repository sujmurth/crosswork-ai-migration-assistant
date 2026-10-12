# qos-migration-agent — build, bundle, and (via UI) onboard the CWAI ADK agent.
IMAGE_NAME = qos-migration-agent
IMAGE_TAG = 1.0.0
PYTHON_SRCS = main.py

.PHONY: all build clean

all: build

build:
	python3 -m py_compile $(PYTHON_SRCS) || ( echo "python sources do not compile" && exit 1 )
	docker build --platform linux/amd64 -f Dockerfile -t $(IMAGE_NAME):$(IMAGE_TAG) -t apps/cisco-crosswork-ai/$(IMAGE_NAME):$(IMAGE_TAG) .
	docker save $(IMAGE_NAME):$(IMAGE_TAG) > image.tar
	tar -cf $(IMAGE_NAME).tar image.tar metadata.json

clean:
	rm -rf $(IMAGE_NAME).tar image.tar __pycache__
