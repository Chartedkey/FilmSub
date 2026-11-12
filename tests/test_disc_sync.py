import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from filmsub import sync
from filmsub.disc import main_feature
from filmsub.srt import Cue, read_srt, write_srt
from filmsub.translator import collect_inputs


def playlist(path, items):
    entries = b''
    for name, start, stop in items:
        item = name.encode() + b'M2TS' + b'\0' * 3 + struct.pack('>II', start * 45000, stop * 45000)
        entries += struct.pack('>H', len(item)) + item
    body = b'\0\0' + struct.pack('>HH', len(items), 0) + entries
    path.write_bytes(b'MPLS0200' + struct.pack('>I', 20) + b'\0' * 8 + struct.pack('>I', len(body)) + body)


class DiscTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def bluray(self):
        b = self.root / 'BDMV'
        (b / 'PLAYLIST').mkdir(parents=True)
        (b / 'STREAM').mkdir()
        for n in ('00001', '00002'):
            (b / 'STREAM' / (n + '.m2ts')).touch()
        playlist(b / 'PLAYLIST/00000.mpls', [('00001', 0, 1)])
        playlist(b / 'PLAYLIST/00001.mpls', [('00002', 10, 20), ('00001', 3, 8)])
        return b

    def test_bluray_order_and_trim(self):
        b = self.bluray()
        name, clips = main_feature(str(self.root))
        self.assertEqual(name, 'Blu-ray 00001.mpls')
        self.assertEqual([(p.name, a, z) for p, a, z in clips],
                         [('00002.m2ts', 10, 20), ('00001.m2ts', 3, 8)])
        self.assertEqual(main_feature(str(b)), (name, clips))

    def test_bad_playlist_not_stream_fallback(self):
        b = self.bluray()
        for p in (b / 'PLAYLIST').iterdir():
            p.write_bytes(b'MPLS0200')
        with self.assertRaises(ValueError):
            main_feature(str(b))

    def test_dvd_selects_largest_title_without_menu(self):
        d = self.root / 'video_ts'
        d.mkdir()
        for name, size in [('VIDEO_TS.VOB', 100), ('VTS_01_0.VOB', 100),
                           ('VTS_01_2.VOB', 20), ('VTS_01_1.VOB', 20), ('VTS_02_1.VOB', 30)]:
            (d / name).write_bytes(b'x' * size)
        name, clips = main_feature(str(self.root))
        self.assertEqual(name, 'DVD VTS_01')
        self.assertEqual([p.name for p, _, _ in clips], ['VTS_01_1.VOB', 'VTS_01_2.VOB'])
        (d / 'VTS_01_1.VOB').unlink()
        (d / 'VTS_01_2.VOB').write_bytes(b'x' * 50)
        with self.assertRaises(ValueError):
            main_feature(str(d))

    def test_discovery_keeps_file_priority(self):
        self.bluray()
        srt = self.root / 'Film.ru.srt'
        self.assertEqual(sync.find_video_for(str(srt)), str(self.root / 'BDMV'))
        (self.root / 'Film.mkv').touch()
        self.assertEqual(sync.find_video_for(str(srt)), str(self.root / 'Film.mkv'))

    def test_single_file_dispatch_unchanged(self):
        cues = [Cue(0, 1000, 'text')]
        with patch.object(sync, '_align_file', return_value=(cues, 'report')) as align:
            self.assertEqual(sync.align('movie.mkv', cues, print), (cues, 'report'))
            align.assert_called_once_with('movie.mkv', cues, print, None, False)

    def test_single_file_alignment_keeps_english_track_selection(self):
        cues = [Cue(i * 1000, i * 1000 + 800, 'text') for i in range(30)]
        shifted = [Cue(c.start_ms + 2000, c.end_ms + 2000, c.text) for c in cues]
        tracks = [sync.AudioTrack(0, 'rus', '', True), sync.AudioTrack(1, 'eng', '', False)]
        with patch.object(sync, 'find_tools', return_value=('alass', 'ffmpeg', 'ffprobe')), patch.object(sync, 'audio_tracks', return_value=tracks), patch.object(sync, '_extract_track') as extract, patch.object(sync, '_alass_once', return_value=shifted), patch.object(sync, 'nosplit_flag', return_value='--no-split'):
            new, report = sync.align('movie.mkv', cues, lambda _: None)
        self.assertEqual(report.status, 'changed')
        self.assertEqual(report.track, 1)
        self.assertEqual(new[0].start_ms, 2000)
        self.assertEqual(extract.call_args.args[:3], ('movie.mkv', 1, 'ffmpeg'))
        self.assertEqual(cues[0].start_ms, 0)

    def test_rejected_does_not_write_or_backup(self):
        p = self.root / 'film.ru.srt'
        write_srt(str(p), [Cue(0, 1000, 'Перевод')])
        before = p.read_bytes()
        with patch.object(sync, 'align', return_value=([Cue(500, 1500, 'other')], sync.SyncReport('rejected', 'bad'))), patch.object(sync, 'backup') as backup:
            sync.sync_subtitles('movie.mkv', str(p), lambda _: None)
            backup.assert_not_called()
        self.assertEqual(p.read_bytes(), before)

    def test_changed_preserves_translation_and_backs_up(self):
        p = self.root / 'film.ru.srt'
        write_srt(str(p), [Cue(0, 1000, 'Перевод')])
        with patch.object(sync, 'align', return_value=([Cue(500, 1500, 'English')], sync.SyncReport('changed', 'ok'))), patch.object(sync, 'backup', return_value='backup') as backup:
            sync.sync_subtitles('movie.mkv', str(p), lambda _: None)
            backup.assert_called_once_with(str(p))
        cue = read_srt(str(p))[0]
        self.assertEqual((cue.start_ms, cue.end_ms, cue.text), (500, 1500, 'Перевод'))

    def test_translation_scan_excludes_disc_clips(self):
        self.bluray()
        source = self.root / 'Film.en.srt'
        source.touch()
        (self.root / 'Film.ru.srt').touch()
        self.assertEqual(collect_inputs([str(self.root)]), [str(source)])

    def test_no_english_and_cancellation(self):
        b = self.bluray()
        with patch.object(sync, 'find_tools', return_value=('alass', 'ffmpeg', 'ffprobe')), patch.object(sync, 'audio_tracks', return_value=[sync.AudioTrack(0, 'rus', '', True)]):
            with self.assertRaises(sync.NoEnglishTrack):
                sync.align(str(b), [], lambda _: None)
            with self.assertRaisesRegex(sync.SyncError, 'Остановлено'):
                sync.align(str(b), [], lambda _: None, stop_flag=lambda: True)

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'requires ffmpeg')
    def test_real_dvd_split_stream_and_ifo_language(self):
        d = self.root / 'VIDEO_TS'
        d.mkdir()
        source = self.root / 'source.vob'
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i',
                        'sine=frequency=440:duration=3', '-c:a', 'ac3', '-f', 'vob', str(source)], check=True)
        data = source.read_bytes()
        split = len(data) // 2 + 17  # VOB boundary inside a packet
        (d / 'VTS_01_1.VOB').write_bytes(data[:split])
        (d / 'VTS_01_2.VOB').write_bytes(data[split:])
        ifo = bytearray(0x204 + 8)
        ifo[:12] = b'DVDVIDEO-VTS'
        struct.pack_into('>H', ifo, 0x202, 1)
        ifo[0x204] = 4
        ifo[0x206:0x208] = b'en'
        (d / 'VTS_01_0.IFO').write_bytes(ifo)
        logs = []
        def check_audio(ref, cues, *args, **kwargs):
            out = subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries',
                                           'format=duration', '-of', 'csv=p=0', ref])
            self.assertAlmostEqual(float(out), 3, delta=.15)
            return cues, sync.SyncReport('unchanged', 'ok')
        with patch.object(sync, 'find_tools', return_value=('alass', 'ffmpeg', 'ffprobe')), patch.object(sync, '_align_file', side_effect=check_audio):
            sync.align(str(d), [Cue(0, 1000, 'text')], logs.append)
        self.assertTrue(any('en' in line for line in logs))

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'requires ffmpeg')
    def test_real_bluray_audio_pipeline(self):
        b = self.bluray()
        # MPEG-TS starts at 11.4 s; MPLS uses the original transport timestamps.
        for n, freq in [('00001', 440), ('00002', 880)]:
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i',
                            f'sine=frequency={freq}:duration=3', '-c:a', 'pcm_bluray',
                            '-mpegts_m2ts_mode', '1', '-output_ts_offset', '10',
                            '-f', 'mpegts', str(b / 'STREAM' / (n + '.m2ts'))], check=True)
        playlist(b / 'PLAYLIST/00001.mpls', [('00002', 12, 13), ('00001', 12, 13)])
        (b / 'PLAYLIST/00000.mpls').unlink()
        def check_audio(ref, cues, *args, **kwargs):
            out = subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries',
                                           'format=duration', '-of', 'csv=p=0', ref])
            self.assertAlmostEqual(float(out), 2, delta=.15)
            return cues, sync.SyncReport('unchanged', 'ok')
        with patch.object(sync, 'find_tools', return_value=('alass', 'ffmpeg', 'ffprobe')), patch.object(sync, '_align_file', side_effect=check_audio):
            sync.align(str(b), [Cue(0, 1000, 'text')], lambda _: None)


if __name__ == '__main__':
    unittest.main()
