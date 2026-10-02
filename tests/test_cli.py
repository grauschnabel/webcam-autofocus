import argparse
import unittest

from webcam_autofocus import cli
from webcam_autofocus.engine import Config


class ConfigFromArgs(unittest.TestCase):
    def parse(self, *argv):
        p = argparse.ArgumentParser()
        cli.add_config_args(p)
        return cli.config_from_args(p.parse_args(argv))

    def test_without_options_it_is_the_default_config(self):
        self.assertEqual(self.parse(), Config())

    def test_options_reach_the_config(self):
        cfg = self.parse("--fmin", "5", "--freeze", "--out", "/dev/video9", "--cooldown", "2.5")
        self.assertEqual((cfg.fmin, cfg.freeze, cfg.out, cfg.cooldown), (5, True, "/dev/video9", 2.5))


if __name__ == "__main__":
    unittest.main()
