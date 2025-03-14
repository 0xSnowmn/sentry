from unittest.mock import patch

import pytest

from sentry.integrations.models.organization_integration import OrganizationIntegration
from sentry.issues.auto_source_code_config.code_mapping import (
    CodeMapping,
    CodeMappingTreesHelper,
    FrameFilename,
    Repo,
    RepoTree,
    UnexpectedPathException,
    UnsupportedFrameFilename,
    convert_stacktrace_frame_path_to_source_path,
    filter_source_code_files,
    find_roots,
    get_extension,
    get_sorted_code_mapping_configs,
    should_include,
)
from sentry.silo.base import SiloMode
from sentry.testutils.cases import TestCase
from sentry.testutils.silo import assume_test_silo_mode
from sentry.utils.event_frames import EventFrame

sentry_files = [
    "bin/__init__.py",
    "bin/example1.py",
    "bin/example2.py",
    "docs-ui/.eslintrc.js",
    "src/sentry/identity/oauth2.py",
    "src/sentry/integrations/slack/client.py",
    "src/sentry/web/urls.py",
    "src/sentry/wsgi.py",
    "src/sentry_plugins/slack/client.py",
]


UNSUPPORTED_FRAME_FILENAMES = [
    "async https://s1.sentry-cdn.com/_static/dist/sentry/entrypoints/app.js",
    "/gtm.js",  # Top source; starts with backslash
    "<anonymous>",
    "<frozen importlib._bootstrap>",
    "[native code]",
    "O$t",
    "async https://s1.sentry-cdn.com/_static/dist/sentry/entrypoints/app.js",
    "/foo/bar/baz",  # no extension
    "README",  # no extension
    "ssl.py",
    # XXX: The following will need to be supported
    "initialization.dart",
    "backburner.js",
]


class TestRepoFiles(TestCase):
    """These evaluate which files should be included as part of a repo."""

    def test_filter_source_code_files(self):
        source_code_files = filter_source_code_files(sentry_files)

        assert source_code_files.index("bin/__init__.py") == 0
        assert source_code_files.index("docs-ui/.eslintrc.js") == 3
        with pytest.raises(ValueError):
            source_code_files.index("README.md")

    def test_filter_source_code_files_not_supported(self):
        source_code_files = filter_source_code_files([])
        assert source_code_files == []
        source_code_files = filter_source_code_files([".env", "README"])
        assert source_code_files == []

    def test_should_not_include(self):
        for file in [
            "static/app/views/organizationRoot.spec.jsx",
            "tests/foo.py",
        ]:
            assert should_include(file) is False


def test_get_extension():
    assert get_extension("") == ""
    assert get_extension("f.py") == "py"
    assert get_extension("f.xx") == "xx"
    assert get_extension("./app/utils/handleXhrErrorResponse.tsx") == "tsx"
    assert get_extension("[native code]") == ""
    assert get_extension("/foo/bar/baz") == ""
    assert get_extension("/gtm.js") == "js"


def test_buckets_logic():
    stacktraces = [
        "app://foo.js",
        "./app/utils/handleXhrErrorResponse.tsx",
        "getsentry/billing/tax/manager.py",
        "/cronscripts/monitoringsync.php",
    ] + UNSUPPORTED_FRAME_FILENAMES
    helper = CodeMappingTreesHelper({})
    buckets = helper._stacktrace_buckets(stacktraces)
    assert buckets == {
        "./app": [FrameFilename("./app/utils/handleXhrErrorResponse.tsx")],
        "app:": [FrameFilename("app://foo.js")],
        "cronscripts": [FrameFilename("/cronscripts/monitoringsync.php")],
        "getsentry": [FrameFilename("getsentry/billing/tax/manager.py")],
    }


class TestFrameFilename:
    def test_frame_filename_package_and_more_than_one_level(self):
        pytest.skip("This test is outdated because of refactors have been made to code mappings")
        # ff = FrameFilename("getsentry/billing/tax/manager.py")
        # assert f"{ff.root}/{ff.dir_path}/{ff.file_name}" == "getsentry/billing/tax/manager.py"
        # assert f"{ff.dir_path}/{ff.file_name}" == ff.file_and_dir_path

    def test_frame_filename_package_and_no_levels(self):
        pytest.skip("This test is outdated because of refactors have been made to code mappings")
        # ff = FrameFilename("root/bar.py")
        # assert f"{ff.root}/{ff.file_name}" == "root/bar.py"
        # assert f"{ff.root}/{ff.file_and_dir_path}" == "root/bar.py"
        # assert ff.dir_path == ""

    def test_frame_filename_repr(self):
        path = "getsentry/billing/tax/manager.py"
        assert FrameFilename(path).__repr__() == f"FrameFilename: {path}"

    def test_raises_unsupported(self):
        for filepath in UNSUPPORTED_FRAME_FILENAMES:
            with pytest.raises(UnsupportedFrameFilename):
                FrameFilename(filepath)

    @pytest.mark.parametrize(
        "files,prefixes",
        [
            ("FrameFilename('app:///utils/something.py').straight_path_prefix", "app:///"),
            ("FrameFilename('./app/utils/something.py').straight_path_prefix", "./"),
            (
                "FrameFilename('../../../../../../packages/something.py').straight_path_prefix",
                "../../../../../../",
            ),
            (
                "FrameFilename('app:///../services/something.py').straight_path_prefix",
                "app:///../",
            ),
        ],
    )
    def test_straight_path_prefix(self, files, prefixes):
        assert eval(files) == prefixes


class TestDerivedCodeMappings(TestCase):
    @pytest.fixture(autouse=True)
    def inject_fixtures(self, caplog):
        self._caplog = caplog

    def setUp(self):
        super().setUp()
        self.foo_repo = Repo("Test-Organization/foo", "master")
        self.bar_repo = Repo("Test-Organization/bar", "main")
        self.code_mapping_helper = CodeMappingTreesHelper(
            {
                self.foo_repo.name: RepoTree(self.foo_repo, files=sentry_files),
                self.bar_repo.name: RepoTree(self.bar_repo, files=["sentry/web/urls.py"]),
            }
        )

        self.expected_code_mappings = [
            CodeMapping(repo=self.foo_repo, stacktrace_root="sentry/", source_path="src/sentry/"),
            CodeMapping(
                repo=self.foo_repo,
                stacktrace_root="sentry_plugins/",
                source_path="src/sentry_plugins/",
            ),
        ]

    def test_package_also_matches(self):
        repo_tree = RepoTree(self.foo_repo, files=["apostello/views/base.py"])
        # We create a new tree helper in order to improve the understability of this test
        cmh = CodeMappingTreesHelper({self.foo_repo.name: repo_tree})
        cm = cmh._generate_code_mapping_from_tree(
            repo_tree=repo_tree, frame_filename=FrameFilename("raven/base.py")
        )
        # We should not derive a code mapping since the package name does not match
        assert cm == []

    def test_no_matches(self):
        stacktraces = [
            "getsentry/billing/tax/manager.py",
            "requests/models.py",
            "urllib3/connectionpool.py",
            "ssl.py",
        ]
        code_mappings = self.code_mapping_helper.generate_code_mappings(stacktraces)
        assert code_mappings == []

    @patch("sentry.issues.auto_source_code_config.code_mapping.logger")
    def test_matches_top_src_file(self, logger):
        stacktraces = ["setup.py"]
        code_mappings = self.code_mapping_helper.generate_code_mappings(stacktraces)
        assert code_mappings == []

    def test_no_dir_depth_match(self):
        code_mappings = self.code_mapping_helper.generate_code_mappings(["sentry/wsgi.py"])
        assert code_mappings == [
            CodeMapping(
                repo=Repo(name="Test-Organization/foo", branch="master"),
                stacktrace_root="sentry/",
                source_path="src/sentry/",
            )
        ]

    def test_more_than_one_match_does_derive(self):
        stacktraces = [
            # More than one file matches for this, however, the package name is taken into account
            # - "src/sentry_plugins/slack/client.py",
            # - "src/sentry/integrations/slack/client.py",
            "sentry_plugins/slack/client.py",
        ]
        code_mappings = self.code_mapping_helper.generate_code_mappings(stacktraces)
        assert code_mappings == [
            CodeMapping(
                repo=self.foo_repo,
                stacktrace_root="sentry_plugins/",
                source_path="src/sentry_plugins/",
            )
        ]

    def test_no_stacktraces_to_process(self):
        code_mappings = self.code_mapping_helper.generate_code_mappings([])
        assert code_mappings == []

    def test_more_than_one_match_works_when_code_mapping_excludes_other_match(self):
        stacktraces = [
            "sentry/identity/oauth2.py",
            "sentry_plugins/slack/client.py",
        ]
        code_mappings = self.code_mapping_helper.generate_code_mappings(stacktraces)
        assert code_mappings == self.expected_code_mappings

    def test_more_than_one_match_works_with_different_order(self):
        stacktraces = [
            # This file matches twice files in the repo, however, the reprocessing
            # feature allows deriving both code mappings
            "sentry_plugins/slack/client.py",
            "sentry/identity/oauth2.py",
        ]
        code_mappings = self.code_mapping_helper.generate_code_mappings(stacktraces)
        assert sorted(code_mappings) == sorted(self.expected_code_mappings)

    @patch("sentry.issues.auto_source_code_config.code_mapping.logger")
    def test_more_than_one_repo_match(self, logger):
        # XXX: There's a chance that we could infer package names but that is risky
        # repo 1: src/sentry/web/urls.py
        # repo 2: sentry/web/urls.py
        stacktraces = ["sentry/web/urls.py"]
        code_mappings = self.code_mapping_helper.generate_code_mappings(stacktraces)
        # The file appears in more than one repo, thus, we are unable to determine the code mapping
        assert code_mappings == []
        logger.warning.assert_called_with("More than one repo matched %s", "sentry/web/urls.py")

    def test_list_file_matches_single(self):
        frame_filename = FrameFilename("sentry_plugins/slack/client.py")
        matches = self.code_mapping_helper.list_file_matches(frame_filename)
        expected_matches = [
            {
                "filename": "src/sentry_plugins/slack/client.py",
                "repo_name": "Test-Organization/foo",
                "repo_branch": "master",
                "stacktrace_root": "sentry_plugins/",
                "source_path": "src/sentry_plugins/",
            }
        ]
        assert matches == expected_matches

    def test_list_file_matches_multiple(self):
        frame_filename = FrameFilename("sentry/web/urls.py")
        matches = self.code_mapping_helper.list_file_matches(frame_filename)
        expected_matches = [
            {
                "filename": "src/sentry/web/urls.py",
                "repo_name": "Test-Organization/foo",
                "repo_branch": "master",
                "stacktrace_root": "sentry/",
                "source_path": "src/sentry/",
            },
            {
                "filename": "sentry/web/urls.py",
                "repo_name": "Test-Organization/bar",
                "repo_branch": "main",
                "stacktrace_root": "",
                "source_path": "",
            },
        ]
        assert matches == expected_matches

    def test_find_roots_starts_with_period_slash(self):
        stacktrace_root, source_path = find_roots("./app/", "static/app/")
        assert stacktrace_root == "./"
        assert source_path == "static/"

    def test_find_roots_starts_with_period_slash_no_containing_directory(
        self,
    ):
        stacktrace_root, source_path = find_roots("./app/", "app/")
        assert stacktrace_root == "./"
        assert source_path == ""

    def test_find_roots_not_matching(self):
        stacktrace_root, source_path = find_roots("sentry/", "src/sentry/")
        assert stacktrace_root == "sentry/"
        assert source_path == "src/sentry/"

    def test_find_roots_equal(self):
        stacktrace_root, source_path = find_roots("source/", "source/")
        assert stacktrace_root == ""
        assert source_path == ""

    def test_find_roots_starts_with_period_slash_two_levels(self):
        stacktrace_root, source_path = find_roots("./app/", "app/foo/app/")
        assert stacktrace_root == "./"
        assert source_path == "app/foo/"

    def test_find_roots_starts_with_app(self):
        stacktrace_root, source_path = find_roots("app:///utils/", "utils/")
        assert stacktrace_root == "app:///"
        assert source_path == ""

    def test_find_roots_starts_with_multiple_dot_dot_slash(self):
        stacktrace_root, source_path = find_roots("../../../../../../packages/", "packages/")
        assert stacktrace_root == "../../../../../../"
        assert source_path == ""

    def test_find_roots_starts_with_app_dot_dot_slash(self):
        stacktrace_root, source_path = find_roots("app:///../services/", "services/")
        assert stacktrace_root == "app:///../"
        assert source_path == ""

    def test_find_roots_bad_stack_path(self):
        with pytest.raises(UnexpectedPathException):
            find_roots("https://yrurlsinyourstackpath.com/", "sentry/something.py")

    def test_find_roots_bad_source_path(self):
        with pytest.raises(UnexpectedPathException):
            find_roots("sentry/random.py", "nothing/something.js")


class TestConvertStacktraceFramePathToSourcePath(TestCase):
    def setUp(self):
        super()
        self.integration, self.oi = self.create_provider_integration_for(
            self.organization, self.user, provider="example", name="Example"
        )

        self.repo = self.create_repo(
            project=self.project,
            name="getsentry/sentry",
        )

        self.code_mapping_empty = self.create_code_mapping(
            organization_integration=self.oi,
            project=self.project,
            repo=self.repo,
            stack_root="",
            source_root="src/",
        )
        self.code_mapping_abs_path = self.create_code_mapping(
            organization_integration=self.oi,
            project=self.project,
            repo=self.repo,
            stack_root="/Users/Foo/src/sentry/",
            source_root="src/sentry/",
        )
        self.code_mapping_file = self.create_code_mapping(
            organization_integration=self.oi,
            project=self.project,
            repo=self.repo,
            stack_root="sentry/",
            source_root="src/sentry/",
        )
        self.code_mapping_backslash = self.create_code_mapping(
            organization_integration=self.oi,
            project=self.project,
            repo=self.repo,
            stack_root="C:\\Users\\Foo\\",
            source_root="/",
        )

    def test_convert_stacktrace_frame_path_to_source_path_empty(self):
        assert (
            convert_stacktrace_frame_path_to_source_path(
                frame=EventFrame(filename="sentry/file.py"),
                code_mapping=self.code_mapping_empty,
                platform="python",
                sdk_name="sentry.python",
            )
            == "src/sentry/file.py"
        )

    def test_convert_stacktrace_frame_path_to_source_path_abs_path(self):
        assert (
            convert_stacktrace_frame_path_to_source_path(
                frame=EventFrame(
                    filename="file.py", abs_path="/Users/Foo/src/sentry/folder/file.py"
                ),
                code_mapping=self.code_mapping_abs_path,
                platform="python",
                sdk_name="sentry.python",
            )
            == "src/sentry/folder/file.py"
        )

    def test_convert_stacktrace_frame_path_to_source_path_java(self):
        assert (
            convert_stacktrace_frame_path_to_source_path(
                frame=EventFrame(filename="File.java", module="sentry.module.File"),
                code_mapping=self.code_mapping_file,
                platform="java",
                sdk_name="sentry.java",
            )
            == "src/sentry/module/File.java"
        )

    def test_convert_stacktrace_frame_path_to_source_path_backslashes(self):
        assert (
            convert_stacktrace_frame_path_to_source_path(
                EventFrame(
                    filename="file.rs", abs_path="C:\\Users\\Foo\\src\\sentry\\folder\\file.rs"
                ),
                code_mapping=self.code_mapping_backslash,
                platform="rust",
                sdk_name="sentry.rust",
            )
            == "src/sentry/folder/file.rs"
        )


class TestGetSortedCodeMappingConfigs(TestCase):
    def setUp(self):
        super()
        with assume_test_silo_mode(SiloMode.CONTROL):
            self.integration = self.create_provider_integration(provider="example", name="Example")
            self.integration.add_organization(self.organization, self.user)
            self.oi = OrganizationIntegration.objects.get(integration_id=self.integration.id)

        self.repo = self.create_repo(
            project=self.project,
            name="getsentry/sentry",
        )
        self.repo.integration_id = self.integration.id
        self.repo.provider = "example"
        self.repo.save()

    def test_get_sorted_code_mapping_configs(self):
        # Created by the user, not well defined stack root
        code_mapping1 = self.create_code_mapping(
            organization_integration=self.oi,
            project=self.project,
            repo=self.repo,
            stack_root="",
            source_root="",
            automatically_generated=False,
        )
        # Created by automation, not as well defined stack root
        code_mapping2 = self.create_code_mapping(
            organization_integration=self.oi,
            project=self.project,
            repo=self.repo,
            stack_root="usr/src/getsentry/src/",
            source_root="",
            automatically_generated=True,
        )
        # Created by the user, well defined stack root
        code_mapping3 = self.create_code_mapping(
            organization_integration=self.oi,
            project=self.project,
            repo=self.repo,
            stack_root="usr/src/getsentry/",
            source_root="",
            automatically_generated=False,
        )
        # Created by the user, not as well defined stack root
        code_mapping4 = self.create_code_mapping(
            organization_integration=self.oi,
            project=self.project,
            repo=self.repo,
            stack_root="usr/src/",
            source_root="",
            automatically_generated=False,
        )
        # Created by automation, well defined stack root
        code_mapping5 = self.create_code_mapping(
            organization_integration=self.oi,
            project=self.project,
            repo=self.repo,
            stack_root="usr/src/getsentry/src/sentry/",
            source_root="",
            automatically_generated=True,
        )
        # Created by user, well defined stack root that references abs_path
        code_mapping6 = self.create_code_mapping(
            organization_integration=self.oi,
            project=self.project,
            repo=self.repo,
            stack_root="/Users/User/code/src/getsentry/src/sentry/",
            source_root="",
            automatically_generated=False,
        )

        # Expected configs: stack_root, automatically_generated
        expected_config_order = [
            code_mapping6,  # "/Users/User/code/src/getsentry/src/sentry/", False
            code_mapping3,  # "usr/src/getsentry/", False
            code_mapping4,  # "usr/src/", False
            code_mapping1,  # "", False
            code_mapping5,  # "usr/src/getsentry/src/sentry/", True
            code_mapping2,  # "usr/src/getsentry/src/", True
        ]

        sorted_configs = get_sorted_code_mapping_configs(self.project)
        assert sorted_configs == expected_config_order
