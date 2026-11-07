"""Seek SDK callback signatures and packed frame-header ABI.

Imported without loading a vendor library; capture owns the live SDK.
"""
import ctypes
from ctypes import (c_void_p, c_int, c_size_t, c_uint32, c_uint8,
                    c_uint16, c_float, c_char, c_char_p, c_uint64, POINTER, CFUNCTYPE)

_seek_signatures = []

FRAME_CALLBACK = CFUNCTYPE(None, c_void_p, c_void_p, c_void_p)
EVENT_CALLBACK = CFUNCTYPE(None, c_void_p, c_int, c_int, c_void_p)

def sig(name, argtypes, restype=c_int):
    _seek_signatures.append((name, argtypes, restype))

sig('seekcamera_error_get_str', [c_int], c_char_p)
sig('seekcamera_manager_get_event_str', [c_int], c_char_p)
sig('seekcamera_manager_create', [POINTER(c_void_p), c_int])
sig('seekcamera_manager_destroy', [POINTER(c_void_p)])
sig('seekcamera_manager_register_event_callback', [c_void_p, EVENT_CALLBACK, c_void_p])
sig('seekcamera_register_frame_available_callback', [c_void_p, FRAME_CALLBACK, c_void_p])
sig('seekcamera_capture_session_start', [c_void_p, c_uint32])
sig('seekcamera_capture_session_stop', [c_void_p])
sig('seekcamera_frame_lock', [c_void_p])
sig('seekcamera_frame_unlock', [c_void_p])
sig('seekframe_get_width',  [c_void_p], c_size_t)
sig('seekframe_get_height', [c_void_p], c_size_t)
sig('seekframe_get_data',   [c_void_p], c_void_p)
sig('seekframe_get_channels',    [c_void_p], c_size_t)
sig('seekframe_get_pixel_depth', [c_void_p], c_size_t)
sig('seekframe_get_line_stride', [c_void_p], c_size_t)
sig('seekframe_get_data_size',   [c_void_p], c_size_t)
sig('seekframe_get_header', [c_void_p], c_void_p)
sig('seekcamera_frame_get_frame_by_format', [c_void_p, c_uint32, POINTER(c_void_p)])
sig('seekcamera_get_chipid', [c_void_p, POINTER(c_char * 16)])

SEEKCAMERA_SHUTTER_MODE_AUTO = 0
SEEKCAMERA_SHUTTER_MODE_MANUAL = 1
SEEKCAMERA_FILTER_SHARPEN_CORRECTION = 2
SEEKCAMERA_COLOR_PALETTE_USER_0 = 11

sig('seekcamera_set_shutter_mode', [c_void_p, c_int])
sig('seekcamera_shutter_trigger',  [c_void_p])
sig('seekcamera_get_color_palette', [c_void_p, POINTER(c_int)])
sig('seekcamera_set_color_palette', [c_void_p, c_int])
sig('seekcamera_color_palette_get_str', [c_int], c_char_p)
sig('seekcamera_get_pipeline_mode', [c_void_p, POINTER(c_int)])
sig('seekcamera_set_pipeline_mode', [c_void_p, c_int])
sig('seekcamera_pipeline_mode_get_str', [c_int], c_char_p)
sig('seekcamera_get_filter_state', [c_void_p, c_int, POINTER(c_int)])
sig('seekcamera_set_filter_state', [c_void_p, c_int, c_int])

# Feature 4: Full Header mapping from seekcamera-simple.cpp
class SeekFrameHeader(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("sentinel", c_uint32),
        ("version", c_uint8),
        ("type", c_uint32),
        ("width", c_uint16),
        ("height", c_uint16),
        ("channels", c_uint8),
        ("pixel_depth", c_uint8),
        ("pixel_padding", c_uint8),
        ("line_stride", c_uint16),
        ("line_padding", c_uint16),
        ("header_size", c_uint16),
        ("timestamp_utc_ns", c_uint64),
        ("chipid", c_char * 16),
        ("serial_number", c_char * 16),
        ("core_part_number", c_char * 32),
        ("firmware_version", c_uint8 * 4),
        ("io_type", c_uint8),
        ("fpa_frame_count", c_uint32),
        ("fpa_diode_count", c_uint32),
        ("environment_temperature", c_float),
        ("thermography_min_x", c_uint16),
        ("thermography_min_y", c_uint16),
        ("thermography_min_value", c_float),
        ("thermography_max_x", c_uint16),
        ("thermography_max_y", c_uint16),
        ("thermography_max_value", c_float),
        ("thermography_spot_x", c_uint16),
        ("thermography_spot_y", c_uint16),
        ("thermography_spot_value", c_float),
        ("agc_mode", c_uint8),
        ("histeq_agc_num_bins", c_uint16),
        ("histeq_agc_bin_width", c_uint16),
        ("histeq_agc_gain_limit_factor", c_float),
        ("histeq_agc_reserved", c_uint16 * 32),
        ("linear_agc_min", c_float),
        ("linear_agc_max", c_float),
        ("linear_agc_reserved", c_float * 32),
        ("gradient_correction_filter_state", c_uint8),
        ("flat_scene_correction_filter_state", c_uint8),
    ]
