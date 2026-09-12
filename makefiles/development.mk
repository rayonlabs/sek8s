.PHONY: install-hooks
install-hooks: ##@development Install git hooks (blocks sensitive pushes to public repo)
	git config core.hooksPath .githooks
	@echo "Git hooks installed. Pre-push guard active."

.PHONY: install-opa
install-opa: ##@development Install OPA binary
	mkdir -p bin
	curl -L -o bin/opa https://openpolicyagent.org/downloads/v1.15.2/opa_linux_amd64_static
	chmod 755 ./bin/opa

.PHONY: install
install: ##@development Instal development dependencies
install: venv install-opa

OPA_POLICIES_DIR ?= ansible/guest/roles/admission-controller/files/policies
OPA_TESTS_DIR ?= tests/opa

.PHONY: test-opa-policies
test-opa-policies: ##@development Run OPA policy tests locally (requires make install)
	./bin/opa test $(OPA_POLICIES_DIR) $(OPA_TESTS_DIR) -v

.PHONY: venv
venv: ##@development Set up virtual environment
venv:
	${POETRY} install

.PHONY: list-packages
list-packages: ##@development Show packages under src/
	@echo $(PACKAGES)

.PHONY: bundle-gpu-tools
bundle-gpu-tools: ##@development Rebuild the vendored nvidia-gpu-tools wheel into the chutes-cvm package (maintainer; needs git + network)
	src/chutes-cvm/tools/gpu-tools/bundle-tools.sh

.PHONY: build-sr25519
build-sr25519: ##@development Build the static sr25519 binary (maintainer; needs cargo + musl target)
	cd src/sr25519 && cargo build --release --target x86_64-unknown-linux-musl
	@echo "built: src/sr25519/target/x86_64-unknown-linux-musl/release/sr25519"

.PHONY: test-sr25519
test-sr25519: ##@development Run the sr25519 Rust tests plus the substrate-interface cross-check
	cd src/sr25519 && cargo test
	${POETRY} run pytest tests/rust -q --no-cov

.PHONY: build
build: ##@development Build the docker images
build: args ?= --network=host --build-arg BUILDKIT_INLINE_CACHE=1
build:
	@if [ -n "$$PROJECT" ]; then \
		pkg_list="$$PROJECT"; \
	else \
		pkg_list="$(PACKAGES)"; \
	fi; \
	for pkg_name in $$pkg_list; do \
		image_dir="docker/$$pkg_name"; \
		pkg_version=$$(if [ -f "src/$$pkg_name/VERSION" ]; then head "src/$$pkg_name/VERSION"; else echo "dev"; fi); \
		if [ ! -f "$$image_dir/Dockerfile" ]; then \
			echo "Skipping $$pkg_name: $$image_dir/Dockerfile not found"; \
			continue; \
		fi; \
		echo "Building images for $$pkg_name (version: $$pkg_version)"; \
		dockerfile="$$image_dir/Dockerfile"; \
		available_targets=$$(grep -i "^FROM.*AS" $$dockerfile | sed 's/.*AS[[:space:]]*\([^[:space:]]*\).*/\1/' | tr '[:upper:]' '[:lower:]' || echo "production development"); \
		for stage_target in $$available_targets; do \
			if [[ "$$stage_target" == production* ]]; then \
				if [[ "$$stage_target" == *-* ]]; then \
					gpu_suffix=$$(echo $$stage_target | sed 's/production-//'); \
					image_tag="$$pkg_version-$$gpu_suffix"; \
					image_name="$$pkg_name"; \
				else \
					image_tag="$$pkg_version"; \
					image_name="$$pkg_name"; \
				fi; \
				echo "Building production target: $$stage_target -> $$image_name:$$image_tag"; \
				DOCKER_BUILDKIT=1 docker build --progress=plain --target $$stage_target \
					-f $$dockerfile \
					-t $$image_name:${BRANCH_NAME}-${BUILD_NUMBER} \
					-t $$image_name:$$image_tag \
					--build-arg PROJECT_DIR=$$pkg_name \
					--build-arg PROJECT=$$pkg_name \
					${args} .; \
			elif [[ "$$stage_target" == development* ]]; then \
				if [[ "$$stage_target" == *-* ]]; then \
					gpu_suffix=$$(echo $$stage_target | sed 's/development-//'); \
					image_tag="$$pkg_version-$$gpu_suffix"; \
					image_name="$${pkg_name}_dev"; \
				else \
					image_tag="$$pkg_version"; \
					image_name="$${pkg_name}_dev"; \
				fi; \
				echo "Building development target: $$stage_target -> $$image_name:$$image_tag"; \
				DOCKER_BUILDKIT=1 docker build --progress=plain --target $$stage_target \
					-f $$dockerfile \
					-t $$image_name:${BRANCH_NAME}-${BUILD_NUMBER} \
					-t $$image_name:$$image_tag \
					--build-arg PROJECT_DIR=$$pkg_name \
					--build-arg PROJECT=$$pkg_name \
					--cache-from $$pkg_name:${BRANCH_NAME}-${BUILD_NUMBER} \
					${args} .; \
			fi; \
		done; \
	done

.PHONY: infrastructure
infrastructure: ##@development Set up infrastructure for tests
infrastructure:
	${DC} up opa registry -d
	./tests/scripts/setup-test-images.sh

.PHONY: clean
clean: ##@development Clean up any dependencies
clean:
	${DC} down opa registry --remove-orphans --volumes
	docker network prune -f
	docker container prune -f

.PHONY: k3s-infrastructure
k3s-infrastructure: ##@development Set up infrastructure for tests
k3s-infrastructure:
	k3d cluster create dev --config docker/config/k3d-config.yml

.PHONY: k3s-clean
k3s-clean: ##@development Clean up any dependencies
k3s-clean:
	k3d cluster delete dev

.PHONY: redeploy
redeploy: ##@development Redeploy infrastructure
redeploy: k3s-clean k3s-infrastructure

.PHONY: ci
ci: ##@development Run CI pipeline
ci: clean build infrastructure lint test clean
