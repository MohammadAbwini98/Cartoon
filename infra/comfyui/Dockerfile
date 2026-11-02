# Animation Remake Studio GPU worker: RunPod serverless ComfyUI worker plus the
# pinned node packs the pilot graph needs. Models are NOT baked in; they live on
# a network volume at /runpod-volume/models (see fetch_models.py), so the image
# stays small and model changes need no rebuild.
#
# Not yet built or run: building and running this image requires a cloud build
# host and GPU, which need the owner's authorization and budget.
ARG BASE_IMAGE=runpod/worker-comfyui:5.11.0-base
FROM ${BASE_IMAGE}

WORKDIR /comfyui/custom_nodes
# name|repo|commit, kept identical to nodes.lock.json (tests/test_infra.py checks this)
RUN set -eux; \
    for spec in \
      "ComfyUI-AnimateDiff-Evolved|https://github.com/Kosinkadink/ComfyUI-AnimateDiff-Evolved|9257651221002dcba0a12f9cff37e1944e58fb60" \
      "ComfyUI-Advanced-ControlNet|https://github.com/Kosinkadink/ComfyUI-Advanced-ControlNet|27a67fee80cf46c198b10588a5651f23d67d1e95" \
      "comfyui_controlnet_aux|https://github.com/Fannovel16/comfyui_controlnet_aux|a587a4c1594524c1d2de5cd1a86cd868ff448e1c" \
      "ComfyUI_IPAdapter_plus|https://github.com/cubiq/ComfyUI_IPAdapter_plus|a0f451a5113cf9becb0847b92884cb10cbdec0ef" \
      "ComfyUI-VideoHelperSuite|https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite|4d907bee61e92c2e65af3bd6383a4e4d356126d1"; \
    do \
      name="${spec%%|*}"; rest="${spec#*|}"; repo="${rest%%|*}"; commit="${rest#*|}"; \
      mkdir "$name" && cd "$name" && git init -q && git fetch -q --depth 1 "$repo" "$commit" && git checkout -q FETCH_HEAD; \
      if [ -f requirements.txt ]; then pip install --no-cache-dir -r requirements.txt; fi; \
      rm -rf .git; cd ..; \
    done

WORKDIR /
