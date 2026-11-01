import importlib.util
from importlib.machinery import SourceFileLoader
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

wrapper_path = Path(__file__).parents[1] / "nixvm"
spec = importlib.util.spec_from_loader("nixvm_wrapper", SourceFileLoader("nixvm_wrapper", str(wrapper_path)))
nixvm_wrapper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nixvm_wrapper)


class ProfileSelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.infra = self.root / "infrastructure"
        self.infra.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_variable_file_stem_selects_profile(self):
        profile_file = self.infra / "workvm.tfvars"
        profile_file.touch()
        with patch.object(nixvm_wrapper, "ROOT", self.root), \
             patch.object(nixvm_wrapper, "INFRA", self.infra):
            self.assertEqual(nixvm_wrapper.profile_name([f"-var-file={profile_file}"]), "workvm")

    def test_variable_arguments_force_name_from_selected_file(self):
        profile_file = self.infra / "workvm.tfvars"
        profile_file.touch()
        with patch.object(nixvm_wrapper, "ROOT", self.root), \
             patch.object(nixvm_wrapper, "INFRA", self.infra), \
             patch.dict(os.environ, {"NIXVM_VAR_FILE": ""}):
            variables, operation = nixvm_wrapper.split_arguments([
                "-var-file", str(profile_file), "-lock-timeout=10s"
            ])
        self.assertEqual(variables, [f"-var-file={profile_file}", "-var=name=workvm"])
        self.assertEqual(operation, ["-lock-timeout=10s"])

    def test_invalid_variable_file_stem_is_rejected(self):
        profile_file = self.infra / "Work VM.tfvars"
        with patch.object(nixvm_wrapper, "ROOT", self.root), \
             patch.object(nixvm_wrapper, "INFRA", self.infra):
            with self.assertRaisesRegex(ValueError, "lowercase VM name"):
                nixvm_wrapper.profile_name([f"-var-file={profile_file}"])

    def test_existing_profile_workspace_is_selected(self):
        with patch.dict(os.environ, {"TF_WORKSPACE": "default", "NIXVM_PROFILE": "default"}), \
             patch.object(nixvm_wrapper, "run", return_value=SimpleNamespace(stdout="  default\n* workvm\n")) as run:
            nixvm_wrapper.ensure_workspace("workvm")
            self.assertEqual(os.environ["TF_WORKSPACE"], "workvm")
            self.assertEqual(run.call_args.args[0][-2:], ["select", "workvm"])

    def test_missing_profile_workspace_is_created(self):
        with patch.dict(os.environ, {"TF_WORKSPACE": "default", "NIXVM_PROFILE": "default"}), \
             patch.object(nixvm_wrapper, "run", return_value=SimpleNamespace(stdout="* default\n")) as run:
            nixvm_wrapper.ensure_workspace("workvm")
            self.assertEqual(os.environ["TF_WORKSPACE"], "workvm")
            self.assertEqual(run.call_args.args[0][-2:], ["new", "workvm"])

    def test_up_auto_approves_tofu_apply(self):
        with patch.dict(os.environ, {}, clear=False), \
             patch.object(nixvm_wrapper, "profile_name", return_value="nixvm"), \
             patch.object(nixvm_wrapper, "ensure_workspace"), \
             patch.object(nixvm_wrapper, "split_arguments", return_value=(
                 ["-var=name=nixvm"], ["-lock-timeout=30s"]
             )), \
             patch.object(nixvm_wrapper, "internal"), \
             patch.object(nixvm_wrapper, "tofu") as tofu, \
             patch.object(nixvm_wrapper, "firewall_tests"), \
             patch.object(nixvm_wrapper, "current_vm_name", return_value="nixvm"), \
             patch.object(nixvm_wrapper, "install_spice_shortcut"):
            nixvm_wrapper.main(["up", "-lock-timeout=30s"])
        tofu.assert_called_once_with("apply", [
            "-var=name=nixvm", "-lock-timeout=30s", "-auto-approve"
        ])

    def test_up_does_not_duplicate_explicit_auto_approve(self):
        with patch.dict(os.environ, {}, clear=False), \
             patch.object(nixvm_wrapper, "profile_name", return_value="nixvm"), \
             patch.object(nixvm_wrapper, "ensure_workspace"), \
             patch.object(nixvm_wrapper, "split_arguments", return_value=(
                 ["-var=name=nixvm"], ["-auto-approve"]
             )), \
             patch.object(nixvm_wrapper, "internal"), \
             patch.object(nixvm_wrapper, "tofu") as tofu, \
             patch.object(nixvm_wrapper, "firewall_tests"), \
             patch.object(nixvm_wrapper, "current_vm_name", return_value="nixvm"), \
             patch.object(nixvm_wrapper, "install_spice_shortcut"):
            nixvm_wrapper.main(["up", "-auto-approve"])
        tofu.assert_called_once_with("apply", ["-var=name=nixvm", "-auto-approve"])



if __name__ == "__main__":
    unittest.main()
