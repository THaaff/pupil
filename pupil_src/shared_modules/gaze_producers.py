'''
(*)~---------------------------------------------------------------------------
Pupil - eye tracking platform
Copyright (C) 2012-2017  Pupil Labs

Distributed under the terms of the GNU
Lesser General Public License (LGPL v3.0).
See COPYING and COPYING.LESSER for license details.
---------------------------------------------------------------------------~(*)
'''

import os
import numpy as np
from copy import deepcopy
from pyglui import ui
from plugin import Producer_Plugin_Base
from player_methods import correlate_data
from methods import normalize
import OpenGL.GL as gl
from pyglui.cygl.utils import *
from pyglui.pyfontstash import fontstash
from glfw import *
from time import time
from calibration_routines import gaze_mapping_plugins
from calibration_routines.finish_calibration import select_calibration_method
from file_methods import load_object, save_object

import gl_utils
import background_helper as bh
import zmq_tools
from itertools import chain, cycle

import logging
logger = logging.getLogger(__name__)

gaze_mapping_plugins_by_name = {p.__name__: p for p in gaze_mapping_plugins}


class Empty(object):
        pass


def setup_fake_pool(frame_size, intrinsics, detection_mode, rec_dir):
    cap = Empty()
    cap.frame_size = frame_size
    cap.intrinsics = intrinsics
    pool = Empty()
    pool.capture = cap
    pool.get_timestamp = time
    pool.detection_mapping_mode = detection_mode
    pool.rec_dir = rec_dir
    pool.app = 'player'
    return pool


colors = cycle(((0.66015625, 0.859375, 0.4609375, 0.8),
                (0.99609375, 0.84375, 0.3984375, 0.8),
                (0.46875, 0.859375, 0.90625, 0.8),
                (0.984375, 0.59375, 0.40234375, 0.8),
                (0.66796875, 0.61328125, 0.9453125, 0.8),
                (0.99609375, 0.37890625, 0.53125, 0.8)))


class Gaze_Producer_Base(Producer_Plugin_Base):
    uniqueness = 'by_base_class'
    order = .02
    icon_chr = chr(0xec14)
    icon_font = 'pupil_icons'

    def init_ui(self):
        self.add_menu()

        gaze_producer_plugins = [p for p in self.g_pool.plugin_by_name.values() if issubclass(p, Gaze_Producer_Base)]
        gaze_producer_plugins.sort(key=lambda p: p.__name__)

        self.menu_icon.order = 0.3

        def open_plugin(p):
            self.notify_all({'subject': 'start_plugin', 'name': p.__name__})

        # We add the capture selection menu
        self.menu.append(ui.Selector(
                                'gaze_producer',
                                setter=open_plugin,
                                getter=lambda: self.__class__,
                                selection=gaze_producer_plugins,
                                labels=[p.__name__.replace('_', ' ') for p in gaze_producer_plugins],
                                label='Gaze Producers'
                            ))

    def recent_events(self, events):
        if 'frame' in events:
            frm_idx = events['frame'].index
            events['gaze_positions'] = self.g_pool.gaze_positions_by_frame[frm_idx]


class Gaze_From_Recording(Gaze_Producer_Base):
    def __init__(self, g_pool):
        super().__init__(g_pool)
        self.result_dir = os.path.join(g_pool.rec_dir, 'offline_data')
        os.makedirs(self.result_dir, exist_ok=True)
        try:
            session_data = load_object(os.path.join(self.result_dir, 'manual_gaze_correction'))
        except OSError:
            session_data = {}
        self.x_offset = session_data.get('dx', 0.)
        self.y_offset = session_data.get('dy', 0.)
        self.load_data_with_offset()

    def load_data_with_offset(self):
        self.g_pool.gaze_positions = deepcopy(self.g_pool.pupil_data['gaze_positions'])
        for gp in self.g_pool.gaze_positions:
            gp['norm_pos'][0] += self.x_offset
            gp['norm_pos'][1] += self.y_offset
        self.g_pool.gaze_positions_by_frame = correlate_data(self.g_pool.gaze_positions, self.g_pool.timestamps)
        self.notify_all({'subject': 'gaze_positions_changed'})
        logger.debug('gaze positions changed')

    def _set_offset_x(self, offset_x):
        self.x_offset = offset_x
        self.notify_all({'subject': 'manual_gaze_correction.offset_changed', 'delay': .5})

    def _set_offset_y(self, offset_y):
        self.y_offset = offset_y
        self.load_data_with_offset()

    def on_notify(self, notification):
        if notification['subject'] == 'manual_gaze_correction.offset_changed':
            self.load_data_with_offset()

    def init_ui(self):
        super().init_ui()
        self.menu.label = "Gaze Data  From Recording"
        self.menu.append(ui.Info_Text('Currently, gaze positions are loaded from the recording.'))
        offset_menu = ui.Growing_Menu('Manual Correction')
        offset_menu.append(ui.Info_Text('The manual correction feature allows you to apply' +
                                        ' a fixed offset to your gaze data.'))
        offset_menu.append(ui.Slider('x_offset', self, min=-.5, step=0.01,
                                     max=.5, setter=self._set_offset_x))
        offset_menu.append(ui.Slider('y_offset', self, min=-.5, step=0.01,
                                     max=.5, setter=self._set_offset_y))
        offset_menu.collapsed = True
        self.menu.append(offset_menu)

    def deinit_ui(self):
        self.remove_menu()

    def cleanup(self):
        session_data = {'dx': self.x_offset, 'dy': self.y_offset}
        save_object(session_data, os.path.join(self.result_dir, 'manual_gaze_correction'))


def calibrate_and_map(g_pool, ref_list, calib_list, map_list, x_offset, y_offset):
    yield "calibrating", []
    method, result = select_calibration_method(g_pool, calib_list, ref_list)
    if result['subject'] != 'calibration.failed':
        logger.info('Offline calibration successful. Starting mapping using {}.'.format(method))
        name, args = result['name'], result['args']
        gaze_mapper_cls = gaze_mapping_plugins_by_name[name]
        gaze_mapper = gaze_mapper_cls(g_pool, **args)

        for idx, datum in enumerate(map_list):
            mapped_gaze = gaze_mapper.on_pupil_datum(datum)

            # apply manual correction
            for gp in mapped_gaze:
                # gp['norm_pos'] is a tuple by default
                gp_norm_pos = list(gp['norm_pos'])
                gp_norm_pos[1] += y_offset
                gp_norm_pos[0] += x_offset
                gp['norm_pos'] = gp_norm_pos

            if mapped_gaze:
                progress = (100 * (idx+1)/len(map_list))
                if progress == 100:
                    progress = "Mapping complete."
                else:
                    progress = "Mapping..{}%".format(int(progress))
                yield progress, mapped_gaze
    else:
        yield "calibration failed", []


def make_section_dict(calib_range, map_range):
        return {'calibration_range': calib_range,
                'mapping_range': map_range,
                'mapping_method': '3d',
                'calibration_method': "circle_marker",
                'status': 'unmapped',
                'color': next(colors),
                'gaze_positions': [],
                'bg_task': None,
                'x_offset': 0.,
                'y_offset': 0.}


class Offline_Calibration(Gaze_Producer_Base):
    session_data_version = 4

    def __init__(self, g_pool, manual_ref_edit_mode=False):
        super().__init__(g_pool)
        self.timeline_line_height = 16
        self.manual_ref_edit_mode = manual_ref_edit_mode
        self.menu = None
        self.process_pipe = None

        self.result_dir = os.path.join(g_pool.rec_dir, 'offline_data')
        os.makedirs(self.result_dir, exist_ok=True)
        try:
            session_data = load_object(os.path.join(self.result_dir, 'offline_calibration_gaze'))
            if session_data['version'] != self.session_data_version:
                logger.warning("Session data from old version. Will not use this.")
                assert False
        except Exception as e:
            map_range = [0, len(self.g_pool.timestamps)]
            calib_range = [len(self.g_pool.timestamps)//10, len(self.g_pool.timestamps)//2]
            session_data = {}
            session_data['sections'] = [make_section_dict(calib_range, map_range), ]
            session_data['circle_marker_positions'] = []
            session_data['manual_ref_positions'] = []
        self.sections = session_data['sections']
        self.circle_marker_positions = session_data['circle_marker_positions']
        self.manual_ref_positions = session_data['manual_ref_positions']
        if self.circle_marker_positions:
            self.detection_progress = 100.0
            for s in self.sections:
                self.calibrate_section(s)
            self.correlate_and_publish()
        else:
            self.detection_progress = 0.0
            self.start_detection_task()

    def append_section(self):
        map_range = [0, len(self.g_pool.timestamps)]
        calib_range = [len(self.g_pool.timestamps)//10, len(self.g_pool.timestamps)//2]
        sec = make_section_dict(calib_range,map_range)
        self.sections.append(sec)
        if self.menu is not None:
            self.append_section_menu(sec)

    def start_detection_task(self):
        self.process_pipe = zmq_tools.Msg_Pair_Server(self.g_pool.zmq_ctx)
        self.circle_marker_positions = []
        source_path = self.g_pool.capture.source_path
        self.notify_all({'subject': 'circle_detector_process.should_start',
                         'source_path': source_path, "pair_url": self.process_pipe.url})

    def init_ui(self):
        super().init_ui()
        self.menu.label = "Offline Calibration"

        self.glfont = fontstash.Context()
        self.glfont.add_font('opensans', ui.get_opensans_font_path())
        self.glfont.set_color_float((1., 1., 1., .8))
        self.glfont.set_align_string(v_align='right', h_align='top')

        def clear_natural_features():
            self.manual_ref_positions = []

        self.menu.append(ui.Info_Text('"Detection" searches for calibration markers in the world video.'))
        # self.menu.append(ui.Button('Redetect', self.start_detection_task))
        slider = ui.Slider('detection_progress', self, label='Detection Progress', setter=lambda _: _)
        slider.display_format = '%3.0f%%'
        self.menu.append(slider)
        self.menu.append(ui.Switch('manual_ref_edit_mode',self,label="Natural feature edit mode"))
        self.menu.append(ui.Button('Clear natural features',clear_natural_features))
        self.menu.append(ui.Button('Add section', self.append_section))

        # set to minimum height
        self.timeline = ui.Timeline('Calibration Sections', self.draw_sections, self.draw_labels, 1)
        self.g_pool.user_timelines.append(self.timeline)

        for sec in self.sections:
            self.append_section_menu(sec)
        self.on_window_resize(glfwGetCurrentContext(), *glfwGetWindowSize(glfwGetCurrentContext()))

    def deinit_ui(self):
        self.remove_menu()
        self.g_pool.user_timelines.remove(self.timeline)
        self.timeline = None
        self.glfont = None

    def append_section_menu(self, sec):
        section_menu = ui.Growing_Menu('Gaze Section')
        section_menu.color = RGBA(*sec['color'])

        def make_validate_fn(sec, key):
            def validate(input_obj):
                try:
                    assert type(input_obj) in (tuple,list)
                    assert type(input_obj[0]) is int
                    assert type(input_obj[1]) is int
                    assert 0 <= input_obj[0] <= input_obj[1] <=len(self.g_pool.timestamps)
                except:
                    pass
                else:
                    sec[key] = input_obj
            return validate

        def make_calibrate_fn(sec):
            def calibrate():
                self.calibrate_section(sec)
            return calibrate

        def make_remove_fn(sec):
            def remove():
                self.timeline.height -= self.timeline_line_height
                del self.menu[self.sections.index(sec)-len(self.sections)]
                del self.sections[self.sections.index(sec)]
                self.correlate_and_publish()

            return remove

        section_menu.append(ui.Selector('calibration_method', sec,
                                        label="Calibration Method",
                                        labels=['Circle Marker', 'Natural Features'],
                                        selection=['circle_marker', 'natural_features']))
        section_menu.append(ui.Selector('mapping_method', sec, label='Calibration Mode',selection=['2d','3d']))
        section_menu.append(ui.Text_Input('status', sec, label='Calbiration Status', setter=lambda _: _))
        section_menu.append(ui.Text_Input('calibration_range', sec, label='Calibration range',
                                          setter=make_validate_fn(sec, 'calibration_range')))
        section_menu.append(ui.Text_Input('mapping_range', sec, label='Mapping range',
                                          setter=make_validate_fn(sec, 'mapping_range')))
        section_menu.append(ui.Button('Recalibrate', make_calibrate_fn(sec)))
        section_menu.append(ui.Button('Remove section', make_remove_fn(sec)))

        # manual gaze correction menu
        offset_menu = ui.Growing_Menu('Manual Correction')
        offset_menu.append(ui.Info_Text('The manual correction feature allows you to apply' +
                                        ' a fixed offset to your gaze data.'))
        offset_menu.append(ui.Slider('x_offset', sec, min=-.5, step=0.01, max=.5))
        offset_menu.append(ui.Slider('y_offset', sec, min=-.5, step=0.01, max=.5))
        offset_menu.collapsed = True
        section_menu.append(offset_menu)

        self.menu.append(section_menu)
        self.timeline.height += self.timeline_line_height

    def get_init_dict(self):
        return {'manual_ref_edit_mode': self.manual_ref_edit_mode}

    def on_notify(self, notification):
        subject = notification['subject']
        if subject == 'pupil_positions_changed':
            for s in self.sections:
                self.calibrate_section(s)

    def on_click(self, pos, button, action):
        if action == GLFW_PRESS and self.manual_ref_edit_mode:
            manual_refs_in_frame = [r for r in self.manual_ref_positions if self.g_pool.capture.get_frame_index() in r['index_range'] ]
            for ref in manual_refs_in_frame:
                if np.sqrt((pos[0]-ref['screen_pos'][0])**2 + (pos[1]-ref['screen_pos'][1])**2) < 15:  # img pixels
                    del self.manual_ref_positions[self.manual_ref_positions.index(ref)]
                    return
            new_ref = { 'screen_pos': pos,
                        'norm_pos': normalize(pos, self.g_pool.capture.frame_size, flip_y=True),
                        'index': self.g_pool.capture.get_frame_index(),
                        'index_range': tuple(range(self.g_pool.capture.get_frame_index()-5,self.g_pool.capture.get_frame_index()+5)),
                        'timestamp': self.g_pool.timestamps[self.g_pool.capture.get_frame_index()]
                        }
            self.manual_ref_positions.append(new_ref)

    def recent_events(self, events):
        super().recent_events(events)

        if self.process_pipe and self.process_pipe.new_data:
            topic, msg = self.process_pipe.recv()
            if topic == 'progress':
                recent = msg.get('data', [])
                progress, data = zip(*recent)
                self.circle_marker_positions.extend([d for d in data if d])
                self.detection_progress = progress[-1]
            elif topic == 'finished':
                self.detection_progress = 100.
                self.process_pipe = None
                for s in self.sections:
                    self.calibrate_section(s)
            elif topic == 'exception':
                logger.warning('Calibration marker detection raised exception:\n{}'.format(msg['reason']))
                self.process_pipe = None
                self.detection_progress = 0.
                logger.info('Marker detection was interrupted')
                logger.debug('Reason: {}'.format(msg.get('reason', 'n/a')))
            self.menu_icon.indicator_stop = self.detection_progress / 100.

        for sec in self.sections:
            if sec["bg_task"]:
                recent = [d for d in sec["bg_task"].fetch()]
                if recent:
                    progress, data = zip(*recent)
                    sec['gaze_positions'].extend(chain(*data))
                    sec['status'] = progress[-1]
                if sec["bg_task"].completed:
                    self.correlate_and_publish()
                    sec['bg_task'] = None

    def correlate_and_publish(self):
        all_gaze = list(chain(*[s['gaze_positions'] for s in self.sections]))
        self.g_pool.gaze_positions = sorted(all_gaze, key=lambda d: d['timestamp'])
        self.g_pool.gaze_positions_by_frame = correlate_data(self.g_pool.gaze_positions, self.g_pool.timestamps)
        self.notify_all({'subject': 'gaze_positions_changed','delay':1})

    def calibrate_section(self,sec):
        if sec['bg_task']:
            sec['bg_task'].cancel()

        sec['status'] = 'starting calibration'#this will be overwritten on sucess
        sec['gaze_positions'] = []  # reset interim buffer for given section

        calib_list = list(chain(*self.g_pool.pupil_positions_by_frame[slice(*sec['calibration_range'])]))
        map_list = list(chain(*self.g_pool.pupil_positions_by_frame[slice(*sec['mapping_range'])]))

        if sec['calibration_method'] == 'circle_marker':
            ref_list = [r for r in self.circle_marker_positions if sec['calibration_range'][0] <= r['index'] <= sec['calibration_range'][1]]
        elif sec['calibration_method'] == 'natural_features':
            ref_list = self.manual_ref_positions
        if not calib_list:
            logger.error('No pupil data to calibrate section "{}"'.format(self.sections.index(sec) + 1))
            sec['status'] = 'calibration failed'
            return

        if not calib_list:
            logger.error('No referece marker data to calibrate section "{}"'.format(self.sections.index(sec) + 1))
            sec['status'] = 'calibration failed'
            return

        if sec["mapping_method"] == '3d' and '2d' in calib_list[len(calib_list)//2]['method']:
            # select median pupil datum from calibration list and use its detection method as mapping method
            logger.warning("Pupil data is 2d, calibration and mapping mode forced to 2d.")
            sec["mapping_method"] = '2d'

        fake = setup_fake_pool(self.g_pool.capture.frame_size, self.g_pool.capture.intrinsics,
                               detection_mode=sec["mapping_method"], rec_dir=self.g_pool.rec_dir)
        generator_args = (fake, ref_list, calib_list, map_list, sec['x_offset'], sec['y_offset'])

        logger.info('Calibrating "{}" in {} mode...'.format(self.sections.index(sec) + 1, sec["mapping_method"]))
        sec['bg_task'] = bh.Task_Proxy('{}'.format(self.sections.index(sec) + 1), calibrate_and_map, args=generator_args)

    def gl_display(self):
        ref_point_norm = [r['norm_pos'] for r in self.circle_marker_positions
                          if self.g_pool.capture.get_frame_index() == r['index']]
        draw_points_norm(ref_point_norm, size=35, color=RGBA(0, .5, 0.5, .7))
        draw_points_norm(ref_point_norm, size=5, color=RGBA(.0, .9, 0.0, 1.0))

        manual_refs_in_frame = [r['norm_pos'] for r in self.manual_ref_positions
                                if self.g_pool.capture.get_frame_index() in r['index_range']]
        draw_points_norm(manual_refs_in_frame, size=35, color=RGBA(.0, .0, 0.9, .8))
        draw_points_norm(manual_refs_in_frame, size=5, color=RGBA(.0, .9, 0.0, 1.0))

    def draw_sections(self, width, height):
        max_ts = len(self.g_pool.timestamps)
        height = len(self.sections) * self.timeline_line_height + 1
        with gl_utils.Coord_System(0, max_ts, 0, height):
            gl.glTranslatef(0, 1 + self.timeline_line_height / 2, 0)
            for s in self.sections:
                color = RGBA(1., 1., 1., .5)
                if s['calibration_method'] == "natural_features":
                    draw_x([(m['index'], 0) for m in self.manual_ref_positions],
                              size=12, color=color)
                else:
                    draw_bars([(m['index'], 0) for m in self.circle_marker_positions],
                              height=12, color=color)
                cal_slc = slice(*s['calibration_range'])
                map_slc = slice(*s['mapping_range'])
                color = RGBA(*s['color'])
                draw_polyline([(cal_slc.start, 0), (cal_slc.stop, 0)], color=color, line_type=gl.GL_LINES, thickness=8)
                draw_polyline([(map_slc.start, 0), (map_slc.stop, 0)], color=color, line_type=gl.GL_LINES, thickness=2)
                gl.glTranslatef(0, self.timeline_line_height, 0)

    def draw_labels(self, width, height):
        self.glfont.set_size(self.timeline_line_height * .8)
        for idx, s in enumerate(self.sections):
            label = 'Calibration Section {}'.format(idx + 1)
            self.glfont.draw_text(width, 0, label)
            gl.glTranslatef(0, self.timeline_line_height, 0)

    def cleanup(self):
        if self.process_pipe:
            self.process_pipe.send(topic='terminate', payload={})
            self.process_pipe.socket.close()
            self.process_pipe = None
        for sec in self.sections:
            if sec['bg_task']:
                sec['bg_task'].cancel()
            sec['bg_task'] = None
            sec["gaze_positions"] = []

        session_data = {}
        session_data['sections'] = self.sections
        session_data['version'] = self.session_data_version
        session_data['manual_ref_positions'] = self.manual_ref_positions
        if self.detection_progress == 100.0:
            session_data['circle_marker_positions'] = self.circle_marker_positions
        else:
            session_data['circle_marker_positions'] = []
        save_object(session_data, os.path.join(self.result_dir, 'offline_calibration_gaze'))
