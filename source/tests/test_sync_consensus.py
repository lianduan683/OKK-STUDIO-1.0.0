"""Independent-window checks for noisy recordings, pauses and false peaks."""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import numpy as np
from audio_sync import _consensus_match, _match_waveform_window, _smooth_envelope, align_audio_segments_fast, _EnergyCache


class SyncConsensusTests(unittest.TestCase):
    def test_one_strong_window_cannot_authorize_a_long_recording(self):
        self.assertIsNone(_consensus_match([(0.,(-42.,.99,.99,.1))],50))

    def test_two_features_from_the_same_window_are_not_independent_evidence(self):
        matches=[(10.,(-42.,.9,.9,.1)),(10.,(-42.02,.8,.8,.1))]
        self.assertIsNone(_consensus_match(matches,50))

    def test_two_consistent_windows_outvote_one_louder_false_peak(self):
        result,support=_consensus_match([(0.,(-42.,.6,.6,.1)),(100.,(-42.04,.55,.55,.1)),
                                         (200.,(-400.,.95,.95,.1))],50)
        self.assertEqual(support,2)
        self.assertAlmostEqual(result[0],-42.,delta=.05)

    def test_two_incompatible_supported_positions_require_manual_review(self):
        matches=[(0.,(-42.,.8,.8,.1)),(50.,(-42.02,.8,.8,.1)),
                 (100.,(-85.,.9,.9,.1)),(150.,(-85.02,.9,.9,.1))]
        self.assertIsNone(_consensus_match(matches,50))

    def test_clock_drift_cannot_be_disguised_as_one_constant_offset(self):
        matches=[(0.,(-42.,.9,.9,.1)),(100.,(-43.,.9,.9,.1)),(200.,(-44.,.9,.9,.1))]
        self.assertIsNone(_consensus_match(matches,50))

    def test_envelope_keeps_common_music_dynamics_under_independent_crowd_noise(self):
        rng=np.random.default_rng(71319)
        base=_smooth_envelope(rng.normal(0,1,300*50),15)*1.3
        master=np.exp(7+base).astype(np.float32)
        offset=42.64
        clean=master[round(offset*50):round((offset+45)*50)]
        noisy=(clean*np.exp(rng.normal(0,.35,len(clean)))).astype(np.float32)
        match=_match_waveform_window(0.,noisy,master,50,0,None)
        self.assertAlmostEqual(match[0],-offset,delta=.08)
        self.assertGreater(match[1],.3)

    def test_internal_edit_cannot_be_reported_as_one_correct_start(self):
        rng=np.random.default_rng(9811)
        master=rng.uniform(100,12000,1000*50).astype(np.float32)
        # One file has an internal 32-second deletion; its two halves require
        # different starts even though both are excellent audio matches.
        camera=np.concatenate((master[30*50:300*50],master[332*50:602*50]))
        with TemporaryDirectory() as folder:
            main=Path(folder)/'main.wav'; clip=Path(folder)/'edited.mov'
            main.touch();clip.touch()
            def read(_self,path,*,start=0.,duration=None,**_kwargs):
                data=master if Path(path)==main else camera
                return data if duration is None else data[round(start*50):round((start+duration)*50)]
            with patch('audio_sync._duration',side_effect=[1000,540]),patch.object(_EnergyCache,'energy',read):
                result=align_audio_segments_fast('ffmpeg',[str(clip)],str(main),continue_on_error=True)[0]
        self.assertEqual(result.confidence,0.)
        self.assertIn('32.00',result.engine)


if __name__=='__main__': unittest.main()
