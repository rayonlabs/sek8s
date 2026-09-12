.PHONY: tag
tag: ##@images Tag docker images from the build step for Parachutes repo
tag:
	@echo "Tagging images for:$$PROJECT"; \
    pkg_name=$$PROJECT; \
	image_dir="docker/$$pkg_name"; \
    if [ -f "src/$$pkg_name/VERSION" ]; then \
        pkg_version=$$(head "src/$$pkg_name/VERSION"); \
    elif [ -f "$$image_dir/VERSION" ]; then \
        pkg_version=$$(head "$$image_dir/VERSION"); \
    else \
        pkg_version="dev"; \
    fi; \
    echo "--------------------------------------------------------"; \
    echo "Tagging $$pkg_name (version: $$pkg_version)"; \
    echo "--------------------------------------------------------"; \
    if [ -d "$$image_dir" ]; then \
        if [ -f "$$image_dir/Dockerfile" ]; then \
            if [ -f "$$image_dir/image.conf" ]; then \
                dockerfile="$$image_dir/Dockerfile"; \
                available_targets=$$(grep -i "^FROM.*AS" $$dockerfile | sed 's/.*AS[[:space:]]*\([^[:space:]]*\).*/\1/' | tr '[:upper:]' '[:lower:]' || echo "production development"); \
                image_conf=$$(cat $$image_dir/image.conf); \
                registry=$$(echo "$$image_conf" | cut -d'/' -f1); \
                image_full_name=$$(echo "$$image_conf" | cut -d'/' -f2); \
                if [[ "$$image_full_name" == *:* ]]; then \
                    image_name=$$(echo "$$image_full_name" | cut -d':' -f1); \
                    image_tag="$$(echo "$$image_full_name" | cut -d':' -f2)-"; \
                else \
                    image_name=$$(echo "$$image_conf" | cut -d'/' -f2); \
                    image_tag=""; \
                fi; \
                if [ "${BRANCH_NAME}" != "main" ]; then \
                    image_tag=$$image_tag$$BRANCH_NAME"-"; \
                fi; \
                for stage_target in $$available_targets; do \
                    if [[ "$$stage_target" == production* ]]; then \
                        if [[ "$$stage_target" == *-* ]]; then \
                            suffix=$$(echo $$stage_target | sed 's/production-//'); \
                            latest_tag="$$image_tag$$suffix-latest"; \
                            target_tag="$$image_tag$$suffix-$$pkg_version"; \
                            src_version=$$pkg_version-$$suffix; \
                        else \
                            latest_tag=$$image_tag"latest"; \
                            target_tag="$$image_tag$$pkg_version"; \
                            src_version=$$pkg_version; \
                        fi; \
                        echo "docker tag $$pkg_name:$$src_version $$registry/$$image_name:$$target_tag"; \
                        docker tag $$pkg_name:$$src_version $$registry/$$image_name:$$target_tag; \
                        echo "docker tag $$pkg_name:$$src_version $$registry/$$image_name:$$latest_tag"; \
                        docker tag $$pkg_name:$$src_version $$registry/$$image_name:$$latest_tag; \
                    fi; \
                done; \
            else \
                echo "Skipping $$pkg_name: $$image_dir/image.conf not found"; \
            fi; \
        else \
            echo "Skipping $$pkg_name: $$image_dir/Dockerfile not found"; \
        fi; \
    else \
        echo "Skipping $$pkg_name: $$image_dir directory not found"; \
    fi; \
    echo ; \

.PHONY: push
push: ##@images Push docker images to registry
push:
	@echo "Pushing images for:$$PROJECT"; \
	pkg_name=$$PROJECT; \
	image_dir=docker/$$pkg_name; \
	if [ -f "src/$$pkg_name/VERSION" ]; then \
		pkg_version=$$(head "src/$$pkg_name/VERSION"); \
	elif [ -f "$$image_dir/VERSION" ]; then \
		pkg_version=$$(head "$$image_dir/VERSION"); \
	else \
		pkg_version="dev"; \
	fi; \
	echo "--------------------------------------------------------"; \
	echo "Pushing $$pkg_name (version: $$pkg_version)"; \
	echo "--------------------------------------------------------"; \
	if [ -f "$$image_dir/Dockerfile" ]; then \
		if [ -f "$$image_dir/image.conf" ]; then \
			dockerfile="$$image_dir/Dockerfile"; \
			available_targets=$$(grep -i "^FROM.*AS" $$dockerfile | sed 's/.*AS[[:space:]]*\([^[:space:]]*\).*/\1/' | tr '[:upper:]' '[:lower:]' || echo "production development"); \
			image_conf=$$(cat $$image_dir/image.conf); \
			registry=$$(echo "$$image_conf" | cut -d'/' -f1); \
			image_full_name=$$(echo "$$image_conf" | cut -d'/' -f2); \
			if [[ "$$image_full_name" == *:* ]]; then \
				image_name=$$(echo "$$image_full_name" | cut -d':' -f1); \
				image_tag="$$(echo "$$image_full_name" | cut -d':' -f2)-"; \
			else \
				image_name=$$(echo "$$image_conf" | cut -d'/' -f2); \
				image_tag=""; \
			fi; \
			if [ "${BRANCH_NAME}" != "main" ]; then \
				image_tag=$$image_tag$$BRANCH_NAME"-"; \
			fi; \
			for stage_target in $$available_targets; do \
				if [[ "$$stage_target" == production* ]]; then \
					if [[ "$$stage_target" == *-* ]]; then \
						suffix=$$(echo $$stage_target | sed 's/production-//'); \
						latest_tag="$$image_tag$$suffix-latest"; \
						target_tag="$$image_tag$$suffix-$$pkg_version"; \
					else \
						latest_tag=$$image_tag"latest"; \
						target_tag="$$image_tag$$pkg_version"; \
					fi; \
					echo "docker push $$registry/$$image_name:$$target_tag"; \
					docker push $$registry/$$image_name:$$target_tag; \
					echo "docker push $$registry/$$image_name:$$latest_tag"; \
					docker push $$registry/$$image_name:$$latest_tag; \
				fi; \
			done; \
		else \
			echo "Skipping $$pkg_name: $$image_dir/image.conf not found"; \
		fi; \
	else \
		echo "Skipping $$pkg_name: $$image_dir/Dockerfile not found"; \
	fi; \
	echo ;

.PHONY: images
images: ##@images Build standalone docker images (all, or one: "make images busybox")
images: args ?= --network=host --build-arg BUILDKIT_INLINE_CACHE=1
images:
	@selected="$(SELECTED_IMGS)"; \
	if [ -z "$$selected" ]; then \
		echo "No standalone (non-source-package) docker images to build."; \
		exit 0; \
	fi; \
	echo "Building standalone images: $$selected"; \
	for pkg_name in $$selected; do \
		image_dir="docker/$$pkg_name"; \
		pkg_version=$$(if [ -f "src/$$pkg_name/VERSION" ]; then head "src/$$pkg_name/VERSION"; elif [ -f "$$image_dir/VERSION" ]; then head "$$image_dir/VERSION"; else echo "dev"; fi); \
		if [ "${BRANCH_NAME}" != "main" ]; then latest_tag="${BRANCH_NAME}-latest"; else latest_tag="latest"; fi; \
		if [ -f "$$image_dir/Dockerfile" ]; then \
			echo "Building images for $$pkg_name (version: $$pkg_version)"; \
			DOCKER_BUILDKIT=1 docker build --progress=plain --target production \
				-f $$image_dir/Dockerfile \
				-t $$pkg_name:${BRANCH_NAME}-${BUILD_NUMBER} \
				-t $$pkg_name:$$pkg_version \
				-t $$pkg_name:$$latest_tag \
				--build-arg PROJECT_DIR=$$pkg_name \
				--build-arg PROJECT=$$pkg_name \
				${args} .; \
			DOCKER_BUILDKIT=1 docker build --progress=plain --target development \
				-f $$image_dir/Dockerfile \
				-t $$pkg_name\_development:${BRANCH_NAME}-${BUILD_NUMBER} \
				-t $$pkg_name\_development:$$pkg_version \
				-t $$pkg_name\_development:$$latest_tag \
				--build-arg PROJECT_DIR=$$pkg_name \
				--build-arg PROJECT=$$pkg_name \
				--cache-from $$pkg_name:${BRANCH_NAME}-${BUILD_NUMBER} \
				${args} .; \
		else \
			echo "Skipping $$pkg_name: $$image_dir/Dockerfile not found"; \
		fi; \
	done


.PHONY: sign
sign: ##@images Sign docker images from the build step for Parachutes repo
sign:
	@if [ -z "$$COSIGN_PRIVATE_KEY" ]; then \
		echo "Error: COSIGN_PRIVATE_KEY environment variable is not set"; \
		echo "Please set COSIGN_PRIVATE_KEY to the path of the Cosign private key (e.g., ~/.cosign/cosign.key)"; \
		exit 1; \
	fi; \
	if [ ! -f "$$COSIGN_PRIVATE_KEY" ]; then \
		echo "Error: COSIGN_PRIVATE_KEY file $$COSIGN_PRIVATE_KEY does not exist"; \
		exit 1; \
	fi; \
	if [ -z "$$COSIGN_PASSWORD" ]; then \
		echo "Enter cosign key password (used for all images in this run):"; \
		read -s COSIGN_PASSWORD; \
		export COSIGN_PASSWORD; \
		echo ""; \
	fi; \
	export COSIGN_PASSWORD; \
	pkg_name=$$PROJECT; \
	image_dir=docker/$$pkg_name; \
	if [ -f "src/$$pkg_name/VERSION" ]; then \
		pkg_version=$$(head "src/$$pkg_name/VERSION"); \
	elif [ -f "$$image_dir/VERSION" ]; then \
		pkg_version=$$(head "$$image_dir/VERSION"); \
	else \
		pkg_version="dev"; \
	fi; \
	echo "--------------------------------------------------------"; \
	echo "Signing $$pkg_name (version: $$pkg_version)"; \
	echo "--------------------------------------------------------"; \
	if [ -f "$$image_dir/Dockerfile" ]; then \
		if [ -f "$$image_dir/image.conf" ]; then \
			dockerfile="$$image_dir/Dockerfile"; \
			available_targets=$$(grep -i "^FROM.*AS" $$dockerfile | sed 's/.*AS[[:space:]]*\([^[:space:]]*\).*/\1/' | tr '[:upper:]' '[:lower:]' || echo "production development"); \
			image_conf=$$(cat $$image_dir/image.conf); \
			registry=$$(echo "$$image_conf" | cut -d'/' -f1); \
			image_full_name=$$(echo "$$image_conf" | cut -d'/' -f2); \
			if [[ "$$image_full_name" == *:* ]]; then \
				image_name=$$(echo "$$image_full_name" | cut -d':' -f1); \
				image_tag="$$(echo "$$image_full_name" | cut -d':' -f2)-"; \
			else \
				image_name=$$(echo "$$image_conf" | cut -d'/' -f2); \
				image_tag=""; \
			fi; \
			if [ "${BRANCH_NAME}" != "main" ]; then \
				image_tag=$$image_tag$$BRANCH_NAME"-"; \
			fi; \
			for stage_target in $$available_targets; do \
				if [[ "$$stage_target" == production* ]]; then \
					if [[ "$$stage_target" == *-* ]]; then \
						suffix=$$(echo $$stage_target | sed 's/production-//'); \
						latest_tag="$$image_tag$$suffix-latest"; \
						target_tag="$$image_tag$$suffix-$$pkg_version"; \
						src_version=$$pkg_version-$$suffix; \
					else \
						latest_tag=$$image_tag"latest"; \
						target_tag="$$image_tag$$pkg_version"; \
						src_version=$$pkg_version; \
					fi; \
					image_ref="$$registry/$$image_name:$$target_tag"; \
					latest_ref="$$registry/$$image_name:$$latest_tag"; \
					echo "Fetching digest for $$image_ref"; \
					digest=$$(docker inspect --format='{{index .RepoDigests 0}}' $$image_ref 2>/dev/null | cut -d'@' -f2 || echo ""); \
					if [ -z "$$digest" ]; then \
						echo "Error: Could not fetch digest for $$image_ref. Ensure the image is pushed and accessible."; \
						continue; \
					fi; \
					echo "cosign sign --yes --key $(COSIGN_PRIVATE_KEY) -a \"org=chutes.ai\" $$registry/$$image_name@$$digest"; \
					cosign sign --yes --key $(COSIGN_PRIVATE_KEY) -a "org=chutes.ai" $$registry/$$image_name@$$digest; \
					latest_digest=$$(docker inspect --format='{{index .RepoDigests 0}}' $$latest_ref 2>/dev/null | cut -d'@' -f2 || echo ""); \
					if [ -n "$$latest_digest" ]; then \
						if [ "$$latest_digest" != "$$digest" ]; then \
							echo "cosign sign --yes --key $(COSIGN_PRIVATE_KEY) -a \"org=chutes.ai\" $$registry/$$image_name@$$latest_digest"; \
							cosign sign --yes --key $(COSIGN_PRIVATE_KEY) -a "org=chutes.ai" $$registry/$$image_name@$$latest_digest; \
						else \
							echo "Skipping latest tag signing: $$latest_tag points to same digest as $$target_tag (already signed)"; \
						fi; \
					else \
						echo "Skipping latest tag signing for $$latest_ref: Digest not found"; \
					fi; \
				fi; \
			done; \
		else \
			echo "Skipping $$pkg_name: $$image_dir/image.conf not found"; \
		fi; \
	else \
		echo "Skipping $$pkg_name: $$image_dir/Dockerfile not found"; \
	fi; \
	echo ;

define _rclone_pass_prompt
	if [ -z "$$RCLONE_CONFIG_PASS" ]; then \
		echo "Enter rclone config password:"; \
		read -s RCLONE_CONFIG_PASS; \
		export RCLONE_CONFIG_PASS; \
		echo ""; \
	fi; \
	export RCLONE_CONFIG_PASS
endef

.PHONY: publish-guest
publish-guest: ##@images Publish built prod guest image + direct-boot artifacts to R2 (ENV=prod)
publish-guest:
	@$(_rclone_pass_prompt); \
	guest-tools/scripts/publish-image.sh --env $(or $(ENV),prod)

.PHONY: publish-guest-debug
publish-guest-debug: ##@images Publish built debug guest image + direct-boot artifacts to R2 (ENV=prod)
publish-guest-debug:
	@$(_rclone_pass_prompt); \
	guest-tools/scripts/publish-image.sh --debug --env $(or $(ENV),prod)
