from tests.conftest import rule_lines

# --- SLOP020: asserts only on its own mocks -----------------------------------


def test_mock_only_test_flagged(write, check):
    write(
        "tests/test_user.py",
        """
        from unittest.mock import Mock, MagicMock

        def test_user_creation():
            user = Mock()
            user.name = "John"
            assert user.name == "John"

        def test_mock_called():
            client = MagicMock()
            client.send("hi")
            client.send.assert_called_once_with("hi")

        class TestThing:
            def test_unittest_style(self):
                m = Mock(return_value=3)
                self.assertEqual(m(), 3)
        """,
    )
    assert rule_lines(check("SLOP020"), "SLOP020") == [3, 8, 14]


def test_patch_param_only_asserted_flagged(write, check):
    write(
        "tests/test_api.py",
        """
        from unittest.mock import patch

        @patch("app.requests.get")
        def test_fetch(mock_get):
            mock_get.return_value.status_code = 200
            assert mock_get.return_value.status_code == 200
        """,
    )
    assert rule_lines(check("SLOP020"), "SLOP020") == [4]


def test_mock_passed_to_real_code_not_flagged(write, check):
    write(
        "tests/test_service.py",
        """
        from unittest.mock import Mock
        from app import Service

        def test_saves():
            repo = Mock()
            Service(repo).run()
            repo.save.assert_called_once()

        def test_compares_real_value():
            m = Mock()
            expected = 3
            assert m.value != expected or compute() == 3
        """,
    )
    assert check("SLOP020").findings == []


def test_mock_helpers_outside_test_files_ignored(write, check):
    write(
        "src/helpers.py",
        """
        from unittest.mock import Mock
        def test_like_helper():
            m = Mock()
            assert m.x == m.x
        """,
    )
    assert check("SLOP020", "SLOP021").findings == []


# --- SLOP021: trivial assertions --------------------------------------------


def test_trivial_assertions_flagged(write, check):
    write(
        "tests/test_misc.py",
        """
        def test_always_passes():
            assert True

        def test_tautology():
            x = 5
            assert x is x

        def test_ignored_result():
            result = compute_total([1, 2])
            assert True

        def test_unittest_true(self):
            self.assertTrue(True)
        """,
    )
    findings = [f for f in check("SLOP021").findings if f.rule == "SLOP021"]
    assert [f.line for f in findings] == [2, 6, 10, 13]
    assert "computes 'result' but never checks it" in findings[2].message


def test_comparisons_that_can_fail_not_flagged(write, check):
    # x == x is False for NaN; properties can return new objects; >= needs __ge__.
    write(
        "tests/test_nan.py",
        """
        def test_nan(value):
            assert value == value

        def test_unittest_nan(self):
            self.assertEqual(self.value, self.value)

        def test_property(obj):
            assert obj.items is obj.items

        def test_ge(a):
            assert a >= a
        """,
    )
    assert check("SLOP021").findings == []


def test_decorator_calls_do_not_count_as_test_code(write, check):
    write(
        "tests/test_param.py",
        """
        import pytest
        from unittest.mock import Mock

        @pytest.mark.parametrize("x", [1, 2])
        def test_param(x):
            assert True

        @pytest.mark.slow()
        def test_mock(m=make_default()):
            user = Mock()
            assert user.name == user.name
            user.save.assert_called()
        """,
    )
    result = check("SLOP020", "SLOP021")
    assert sorted((f.rule, f.line) for f in result.findings) == [("SLOP020", 9), ("SLOP021", 6)]


def test_wrapped_mock_runs_real_code(write, check):
    write(
        "tests/test_wraps.py",
        """
        from unittest.mock import Mock
        from app import real_function

        def test_wraps():
            m = Mock(wraps=real_function)
            assert m(1) == 2
        """,
    )
    assert check("SLOP020").findings == []


def test_smoke_test_with_assert_true_not_flagged(write, check):
    # numpy has tests like this: calling the code is the test ("doesn't crash").
    write(
        "tests/test_smoke.py",
        """
        def test_gh28014(self):
            self.module.inquire_next(3)
            assert True

        def test_real():
            assert compute() == 3
            assert True
        """,
    )
    assert check("SLOP021").findings == []


def test_pytest_raises_not_flagged(write, check):
    write(
        "tests/test_err.py",
        """
        import pytest

        def test_raises():
            with pytest.raises(ValueError):
                parse("x")
            assert True
        """,
    )
    assert check("SLOP021").findings == []


# --- SLOP022: swallowed assertions ------------------------------------------


def test_swallowed_assertions_flagged(write, check):
    write(
        "tests/test_swallow.py",
        """
        def test_cannot_fail():
            try:
                assert validate("x") is False
            except Exception:
                pass

        def test_bare_except():
            try:
                assert compute() == 1
            except:
                print("failed")

        def test_assertion_error():
            try:
                assert compute() == 1
            except AssertionError as e:
                log(e)

        def test_specific_then_broad():
            try:
                assert compute() == 1
            except KeyError:
                raise
            except Exception:
                pass
        """,
    )
    assert rule_lines(check("SLOP022"), "SLOP022") == [4, 10, 16, 24]


def test_swallow_not_flagged_when_reraised_or_other_asserts(write, check):
    write(
        "tests/test_ok.py",
        """
        import pytest

        def test_reraises():
            try:
                assert compute() == 1
            except Exception:
                cleanup()
                raise

        def test_pytest_fail():
            try:
                assert compute() == 1
            except Exception as e:
                pytest.fail(str(e))

        def test_fail_on_a_call_result(self):
            try:
                assert compute() == 1
            except Exception as e:
                self.reporter().fail(str(e))

        def test_specific_exception():
            try:
                assert compute() == 1
            except KeyError:
                pass

        def test_earlier_handler_reraises():
            try:
                assert compute() == 1
            except AssertionError:
                raise
            except Exception:
                pass

        def test_unknown_exception_type_first():
            try:
                assert compute() == 1
            except MyCustomError:
                pass
            except Exception:
                pass

        def test_another_assert_can_fail():
            # psutil does this: one tolerant check, one strict check.
            try:
                assert exe() == "python"
            except AssertionError:
                pass
            assert run() == "hey"
        """,
    )
    assert check("SLOP022").findings == []
