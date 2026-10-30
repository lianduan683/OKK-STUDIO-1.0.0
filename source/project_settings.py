"""Project format preferences shared by the editor, preview and renderer."""
RESOLUTIONS = {
    '1920 × 1080 HD': (1920, 1080), '2560 × 1440 QHD': (2560, 1440),
    '3840 × 2160 Ultra HD': (3840, 2160), '4096 × 2160 DCI 4K': (4096, 2160),
    '7680 × 4320 8K': (7680, 4320), '1080 × 1920 竖屏': (1080, 1920),
    '自定义': None,
}
FRAME_RATES = ('23.976', '24', '25', '29.97', '30', '48', '50', '59.94', '60', '96', '100', '119.88', '120', '144', '240')
SAMPLE_RATES = (44100, 48000, 88200, 96000, 176400, 192000)
BIT_DEPTHS = {'16bit': 'pcm_s16le', '24bit': 'pcm_s24le',
              '32bit': 'pcm_s32le', '32bit 浮点': 'pcm_f32le'}
DEFAULT_SETTINGS = dict(width=1920, height=1080, sample_rate=48000, bit_depth='24bit')


def validate_project_settings(value):
    if not isinstance(value, dict): raise ValueError('工程设置必须是对象。')
    if not value: return
    if set(value) != set(DEFAULT_SETTINGS): raise ValueError('工程设置字段不完整或包含未知字段。')
    for name in ('width', 'height'):
        if type(value[name]) is not int or not 16 <= value[name] <= 16384 or value[name] % 2:
            raise ValueError('时间线宽度和高度须为 16–16384 之间的偶数。')
    if type(value['sample_rate']) is not int or value['sample_rate'] not in SAMPLE_RATES:
        raise ValueError('请选择有效的音频采样率。')
    if not isinstance(value['bit_depth'], str) or value['bit_depth'] not in BIT_DEPTHS:
        raise ValueError('请选择有效的音频位深。')


def timeline_dimensions(project, fallback):
    settings = getattr(project, 'project_settings', {})
    return (settings['width'], settings['height']) if settings else fallback


def validate_monitor_layout(value):
    if not isinstance(value, dict): raise ValueError('监看布局必须是对象。')
    if set(value) - {'rows', 'columns', 'page'}: raise ValueError('监看布局含未知字段。')
    for name in ('rows', 'columns'):
        if name in value and (type(value[name]) is not int or not 1 <= value[name] <= 6):
            raise ValueError('监看行列数须为 1–6。')
    if 'page' in value and (type(value['page']) is not int or not 0 <= value['page'] <= 100000):
        raise ValueError('监看页码无效。')
