# Copyright (c) 2022, NVIDIA CORPORATION & AFFILIATES.  All rights reserved.
#
# NVIDIA CORPORATION & AFFILIATES and its licensors retain all intellectual property
# and proprietary rights in and to this software, related documentation
# and any modifications thereto.  Any use, reproduction, disclosure or
# distribution of this software and related documentation without an express
# license agreement from NVIDIA CORPORATION & AFFILIATES is strictly prohibited.

from __future__ import annotations
from typing import Optional, Dict
import torch
from wisp.core import RenderBuffer, Rays, PrimitivesPack
from wisp.renderer.core.api import RayTracedRenderer, FramePayload, field_renderer
from wisp.models.nefs.nerf import NeuralRadianceField, BaseNeuralField
from wisp.tracers import PackedRFTracer
from wisp.accelstructs import OctreeAS, AxisAlignedBBoxAS
from wisp.gfx.datalayers import Datalayers, OctreeDatalayers, AABBDatalayers


@field_renderer(BaseNeuralField, PackedRFTracer)
class NeuralRadianceFieldPackedRenderer(RayTracedRenderer):
    """ A neural field renderers for pipelines of NeuralRadianceField + PackedRFTracer.
        The renderer is registered with the general BaseNeuralField to make a default fallback for future neural field
        subclasses which use the PackedRFTracer and don't implement a dedicated renderer.
    """

    def __init__(self, nef: NeuralRadianceField, tracer_type=None, batch_size=2**14, num_steps=None,
                 min_dis=None, raymarch_type=None, *args, **kwargs):
        super().__init__(nef, *args, **kwargs)
        self.batch_size = batch_size
        if num_steps is None:
            num_steps = 2
        self.num_steps = num_steps
        self.num_steps_movement = max(num_steps // 4, 1)
        if raymarch_type is None:
            raymarch_type = 'voxel'
        self.raymarch_type = raymarch_type
        self.bg_color = 'black'

        self.tracer = tracer_type() if tracer_type is not None else PackedRFTracer()
        self.render_res_x = None
        self.render_res_y = None
        self.output_width = None
        self.output_height = None
        self.far_clipping = None
        self.channels = None
        self._last_state = dict()

        self._data_layers = self.regenerate_data_layers()

    @classmethod
    def create_layers_painter(cls, nef: BaseNeuralField) -> Optional[Datalayers]:
        """ NeuralRadianceFieldPackedRenderer can draw datalayers showing the occupancy status.
        These depend on the bottom level acceleration structure.
        """
        if not hasattr(nef.grid, 'blas'):
            return None
        elif isinstance(nef.grid.blas, AxisAlignedBBoxAS):
            return AABBDatalayers()
        elif isinstance(nef.grid.blas, OctreeAS):
            return OctreeDatalayers()
        else:
            return None

    def needs_redraw(self) -> bool:
        if self.layers_painter is not None:
            return self.layers_painter.needs_redraw(self.nef.grid.blas)
        else:
            return True

    def regenerate_data_layers(self) -> Dict[str, PrimitivesPack]:
        if self.layers_painter is not None:
            return self.layers_painter.regenerate_data_layers(self.nef.grid.blas)
        else:
            return dict()

    def pre_render(self, payload: FramePayload, *args, **kwargs) -> None:
        super().pre_render(payload)
        self.render_res_x = payload.render_res_x
        self.render_res_y = payload.render_res_y
        self.output_width = payload.camera.width
        self.output_height = payload.camera.height
        self.far_clipping = payload.camera.far
        self.bg_color = 'black' if payload.clear_color == (0.0, 0.0, 0.0) else 'white'
        if payload.interactive_mode:
            self.tracer.num_steps = self.num_steps_movement
        else:
            self.tracer.num_steps = self.num_steps
        self.channels = payload.channels

    def needs_refresh(self, payload: FramePayload, *args, **kwargs) -> bool:
        return self._last_state.get('num_steps', 0) < self.num_steps or \
               self._last_state.get('channels') != self.channels

    def render(self, rays: Optional[Rays] = None) -> RenderBuffer:
        rb = RenderBuffer(hit=None)
        for ray_batch in rays.split(self.batch_size):
            # TODO(ttakikawa): Add a way to control the LOD in the GUI
            rb += self.tracer(self.nef, channels=self.channels,
                              rays=ray_batch, lod_idx=None, raymarch_type=self.raymarch_type,
                              num_steps=self.num_steps, bg_color=self.bg_color)

        # Rescale renderbuffer to original size
        rb = rb.reshape(self.render_res_y, self.render_res_x, -1)
        if self.render_res_x != self.output_width or self.render_res_y != self.output_height:
            rb = rb.scale(size=(self.output_height, self.output_width))
        return rb

    def post_render(self) -> None:
        self._last_state['num_steps'] = self.tracer.num_steps
        self._last_state['channels'] = self.channels

    @property
    def dtype(self) -> torch.dtype:
        return torch.float32

    def name(self) -> str:
        """
        Returns:
            (str) A a meaningful, human readable name representing the object this renderer paints.
        """
        return "Neural Radiance Field"
