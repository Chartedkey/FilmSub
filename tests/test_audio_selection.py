from pathlib import Path
import struct
import shutil
import subprocess
import array
import tempfile
import unittest
from unittest.mock import Mock, patch

from filmsub import sync
from filmsub.disc import bluray_languages
from filmsub.gui_audio import track_label
from filmsub.srt import Cue, write_srt


class AudioSelectionTests(unittest.TestCase):
    def setUp(self):
        self.tracks = [sync.AudioTrack(0, 'ru', '', True, 0x1100),
                       sync.AudioTrack(1, 'de', '', False, 0x1101),
                       sync.AudioTrack(2, 'fr', 'Original', False, 0x1102)]
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_explicit_french_overrides_default_and_english(self):
        tracks = self.tracks + [sync.AudioTrack(3, 'eng', '', False)]
        self.assertIs(sync.select_audio(tracks, lambda _: 2), tracks[2])
        self.assertIs(sync.select_audio(tracks), tracks[3])

    def test_arbitrary_language_and_unmarked_track(self):
        for lang in ['fr', 'ja', 'ko', 'it', 'spa', 'xyz', 'und', '']:
            track = sync.AudioTrack(4, lang, '', False)
            self.assertIs(sync.select_audio([track], lambda _: 4), track)

    def test_invalid_selection_is_error(self):
        with self.assertRaises(sync.SyncError):
            sync.select_audio(self.tracks, lambda _: 9)
        with self.assertRaises(sync.SyncError):
            sync.select_audio([], lambda _: 0)

    def test_cancel_does_not_touch_subtitles(self):
        source = self.root / 'Film.ru.srt'
        write_srt(str(source), [Cue(0, 1000, 'Перевод')])
        original = source.read_bytes()
        with patch.object(sync, 'find_tools', return_value=('alass', 'ffmpeg', 'ffprobe')), patch.object(sync, 'audio_tracks', return_value=self.tracks), patch.object(sync, '_alass_once') as alass, patch.object(sync, 'backup') as backup:
            with self.assertRaises(sync.SyncCancelled):
                sync.sync_subtitles('Film.mkv', str(source), print, choose_track=lambda _: None)
            alass.assert_not_called()
            backup.assert_not_called()
        self.assertEqual(source.read_bytes(), original)

    def test_file_extracts_selected_french_audio(self):
        cues = [Cue(i * 1000, i * 1000 + 800, 'Перевод') for i in range(30)]
        with patch.object(sync, 'find_tools', return_value=('alass', 'ffmpeg', 'ffprobe')), patch.object(sync, 'audio_tracks', return_value=self.tracks), patch.object(sync, '_extract_track') as extract, patch.object(sync, '_alass_once', return_value=cues), patch.object(sync, 'nosplit_flag', return_value='--no-split'):
            _, report = sync.align('Film.mkv', cues, lambda _: None, choose_track=lambda _: 2)
        self.assertEqual(extract.call_args.args[:3], ('Film.mkv', 2, 'ffmpeg'))
        self.assertEqual(report.track, 2)
        self.assertEqual(report.status, 'unchanged')

    def disc(self):
        (self.root / 'BDMV').mkdir()
        clips = [(self.root / 'first.m2ts', 0., 2.), (self.root / 'second.m2ts', 0., 2.)]
        return clips

    def test_bluray_asks_once_and_preserves_pid_when_order_changes(self):
        clips = self.disc()
        reordered = [sync.AudioTrack(0, 'fr', 'Original', False, 0x1102),
                     sync.AudioTrack(1, 'ru', '', True, 0x1100)]
        chooser = Mock(return_value=2)
        process = Mock(returncode=0)
        with patch('filmsub.disc.main_feature', return_value=('Blu-ray 00001.mpls', clips)), patch.object(sync, 'find_tools', return_value=('alass', 'ffmpeg', 'ffprobe')), patch.object(sync, 'audio_tracks', side_effect=[self.tracks, reordered]), patch.object(sync, '_extract_track') as extract, patch.object(sync.subprocess, 'Popen', return_value=process), patch.object(sync, '_wait'), patch.object(sync, '_align_file', return_value=([], sync.SyncReport('unchanged', 'ok'))):
            _, report = sync.align(str(self.root), [], lambda _: None, choose_track=chooser)
        chooser.assert_called_once()
        self.assertEqual([c.args[1] for c in extract.call_args_list], [2, 0])
        self.assertEqual(report.track, 2)

    def test_bluray_missing_selected_audio_never_falls_back(self):
        clips = self.disc()
        with patch('filmsub.disc.main_feature', return_value=('Blu-ray 00001.mpls', clips)), patch.object(sync, 'find_tools', return_value=('alass', 'ffmpeg', 'ffprobe')), patch.object(sync, 'audio_tracks', side_effect=[self.tracks, self.tracks[:2]]), patch.object(sync, '_extract_track') as extract:
            with self.assertRaisesRegex(sync.SyncError, 'Выбранная дорожка'):
                sync.align(str(self.root), [], lambda _: None, choose_track=lambda _: 2)
        self.assertEqual(extract.call_count, 1)

    def test_dvd_choice_receives_ifo_languages(self):
        d = self.root / 'VIDEO_TS'
        d.mkdir()
        clip = d / 'VTS_01_1.VOB'
        chooser = Mock(return_value=2)
        process = Mock(returncode=0)
        tracks = [sync.AudioTrack(i, '', '', False) for i in range(3)]
        with patch('filmsub.disc.main_feature', return_value=('DVD VTS_01', [(clip, None, None)])), patch('filmsub.disc.dvd_languages', return_value=['ru', 'de', 'fr']), patch.object(sync, 'find_tools', return_value=('alass', 'ffmpeg', 'ffprobe')), patch.object(sync, 'audio_tracks', return_value=tracks), patch.object(sync, '_extract_track') as extract, patch.object(sync.subprocess, 'Popen', return_value=process), patch.object(sync, '_wait'), patch.object(sync, '_align_file', return_value=([], sync.SyncReport('unchanged', 'ok'))):
            sync.align(str(d), [], lambda _: None, choose_track=chooser)
        self.assertEqual([t.lang for t in chooser.call_args.args[0]], ['ru', 'de', 'fr'])
        self.assertEqual(extract.call_args.args[1], 2)

    def test_read_bluray_languages_by_pid(self):
        b = self.root / 'BDMV'
        (b / 'CLIPINF').mkdir(parents=True)
        streams = b''
        for pid, lang in [(0x1100, b'rus'), (0x1102, b'fra')]:
            coding = b'\x81\x31' + lang
            streams += struct.pack('>HB', pid, len(coding)) + coding
        program = b'\0\x01' + struct.pack('>IHBB', 0, 0x100, 2, 0) + streams
        data = b'HDMV0200' + struct.pack('>II', 0, 16) + struct.pack('>I', len(program)) + program
        info = b / 'CLIPINF/00001.clpi'
        info.write_bytes(data)
        self.assertEqual(bluray_languages(str(b), '00001.m2ts'), {0x1100: 'rus', 0x1102: 'fra'})
        info.write_bytes(data[:20])
        self.assertEqual(bluray_languages(str(b), '00001.m2ts'), {})

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'requires ffmpeg')
    def test_real_file_uses_french_audio_instead_of_russian(self):
        source = self.root / 'Film.mkv'
        command = ['ffmpeg', '-v', 'error', '-y']
        for freq in (300, 600, 900):
            command += ['-f', 'lavfi', '-i', f'sine=frequency={freq}:duration=3']
        command += ['-map', '0:a', '-map', '1:a', '-map', '2:a', '-c:a', 'flac',
                    '-metadata:s:a:0', 'language=rus', '-metadata:s:a:1', 'language=deu',
                    '-metadata:s:a:2', 'language=fra', str(source)]
        subprocess.run(command, check=True)
        cues = [Cue(i * 1000, i * 1000 + 800, 'Перевод') for i in range(30)]
        def check_ref(alass, ref, *args):
            raw = subprocess.check_output(['ffmpeg', '-v', 'error', '-i', ref, '-f',
                                           's16le', '-ac', '1', '-ar', '16000', '-'])
            samples = array.array('h', raw)
            crossings = sum(a <= 0 < b for a, b in zip(samples, samples[1:]))
            frequency = crossings / (len(samples) / 16000)
            self.assertAlmostEqual(frequency, 900, delta=5)
            return cues
        def choose(tracks):
            self.assertEqual([t.lang for t in tracks], ['rus', 'deu', 'fra'])
            return 2
        with patch.object(sync, 'find_tools', return_value=('alass', 'ffmpeg', 'ffprobe')), patch.object(sync, '_alass_once', side_effect=check_ref), patch.object(sync, 'nosplit_flag', return_value='--no-split'):
            _, report = sync.align(str(source), cues, lambda _: None, choose_track=choose)
        self.assertEqual(report.track, 2)

    def test_track_labels_identify_french_and_unmarked(self):
        self.assertEqual(track_label(self.tracks[2]), '№3 — французский (fr) «Original»')
        self.assertIn('язык не указан', track_label(sync.AudioTrack(0, '', '', False)))
        self.assertIn('xyz', track_label(sync.AudioTrack(0, 'xyz', '', False)))


if __name__ == '__main__':
    unittest.main()
