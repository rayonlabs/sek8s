SHELL := /bin/bash -e -o pipefail
export PATH := $(HOME)/.local/bin:$(PATH)
PROJECT ?=
BRANCH_NAME ?= $(shell git rev-parse --abbrev-ref HEAD | tr '/' '-')
BUILD_NUMBER ?= 0
IMAGE ?= ${PROJECT}:${BRANCH_NAME}-${BUILD_NUMBER}
COMPOSE_FILE=docker/docker-compose.yaml
COMPOSE_BASE_FILE=docker/docker-compose.base.yaml
DC=docker compose -p ${PROJECT} -f ${COMPOSE_FILE} -f ${COMPOSE_BASE_FILE}
POETRY ?= "poetry"

SRC_DIR := src
# Python packages only: the Python tooling below derives src/<pkg>/<import>/ paths and
# -p/--cov import names from this list, so non-Python packages under src/ (e.g. the
# sr25519-signer Rust crate) must not appear in it.
PACKAGES := $(patsubst $(SRC_DIR)/%/pyproject.toml,%,$(wildcard $(SRC_DIR)/*/pyproject.toml))
VERSION := $(shell head ansible/guest/VERSION | grep -Eo "\d+.\d+.\d+")

# Package filter: "make <target> sek8s" selects one package
PKG_FILTER := $(filter $(PACKAGES),$(MAKECMDGOALS))
SELECTED_PKGS := $(or $(PKG_FILTER),$(PACKAGES))

# Standalone docker images: docker/<name> with a Dockerfile and no matching src/ package.
# Image filter: "make images busybox" selects one image; bare "make images" builds all.
STANDALONE_IMAGES := $(shell for d in docker/*/; do n=$$(basename "$$d"); [ -f "$$d/Dockerfile" ] && [ ! -d "src/$$n" ] && echo $$n; done)
IMG_FILTER := $(filter $(STANDALONE_IMAGES),$(MAKECMDGOALS))
SELECTED_IMGS := $(or $(IMG_FILTER),$(STANDALONE_IMAGES))

# Wire package/image goal into PROJECT for docker targets (build/tag/push/sign)
ifeq ($(PROJECT),)
ifneq ($(PKG_FILTER),)
override PROJECT := $(firstword $(PKG_FILTER))
else ifneq ($(IMG_FILTER),)
override PROJECT := $(firstword $(IMG_FILTER))
endif
endif

pkg_to_import = $(subst -,_,$(1))

# Source dirs for selected packages: src/<pkg>/<import>/
SRC_DIRS := $(foreach pkg,$(SELECTED_PKGS),$(SRC_DIR)/$(pkg)/$(call pkg_to_import,$(pkg)))

# Include tests/ when sek8s is selected (tests live at root)
ifneq ($(filter sek8s,$(SELECTED_PKGS)),)
LINT_DIRS := $(SRC_DIRS) tests
else
LINT_DIRS := $(SRC_DIRS)
endif

COV_ARGS := $(foreach pkg,$(SELECTED_PKGS),--cov=$(call pkg_to_import,$(pkg)))
MYPY_ARGS := $(foreach pkg,$(SELECTED_PKGS),-p $(call pkg_to_import,$(pkg)))

# Allow package names as make goals (no-op targets)
ifneq ($(PKG_FILTER),)
$(PKG_FILTER):
	@:
endif

# Allow standalone image names as make goals (no-op targets)
ifneq ($(IMG_FILTER),)
$(IMG_FILTER):
	@:
endif

.DEFAULT_GOAL := help

.EXPORT_ALL_VARIABLES:

include makefiles/changelog.mk
include makefiles/development.mk
include makefiles/images.mk
include makefiles/help.mk
include makefiles/lint.mk
include makefiles/local.mk
include makefiles/test.mk
-include makefiles/security.mk
