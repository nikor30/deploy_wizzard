"""The hint attached to a Catalyst Center 'invalid CLI' failure must match what
the switch actually did.

CCC always appends the prompts it *expects* ("… [y/n] (Interactive) ACCEPT?
(yes/[no]): (Interactive) [confirm] (Interactive)"). Matching "(Interactive)"
in that list sent every invalid-CLI failure to the AAA/C3PL conversion advice -
including a live run where `switchport mode access` simply cut the SSH session.
"""

from app.services.dayn import interactive_prompt_hint

EXPECTS = (
    "Current expects : ssto145cis.{0,30}([^)]+)#s*z (Config Prompt) ssto145cis#s*z (Prompt) "
    "[y/n] (Interactive) ACCEPT? (yes/[no]): (Interactive) [confirm] (Interactive) <br></pre>"
)
PREFIX = (
    "NCTP10214: Provisioning failed for the template IT_DayN_Port_Template.<pre>Message: "
    "Unable to push the invalid CLI to the device 172.20.10.145 using protocol ssh2. "
    "Invalid CLI - Current output : "
)


def test_silent_switch_after_the_command_points_at_the_management_path() -> None:
    # the live failure: only the echoed command came back, no prompt at all
    hint = interactive_prompt_hint(f"{PREFIX}switchport mode access {EXPECTS}")
    assert "switchport mode access" in hint
    assert "stopped answering" in hint
    assert "management path" in hint
    assert "#INTERACTIVE" not in hint  # not the AAA conversion advice


def test_a_real_prompt_in_the_output_still_gets_the_interactive_advice() -> None:
    output = (
        "class-map type control subscriber match-all AAA_SVR_DOWN_AUTHD_HOST This operation "
        "will permanently convert all relevant authentication commands. Do you wish to "
        "continue? [yes]: "
    )
    hint = interactive_prompt_hint(f"{PREFIX}{output}{EXPECTS}")
    assert "#INTERACTIVE" in hint
    assert "stopped answering" not in hint


def test_an_ios_rejection_gets_no_misleading_hint() -> None:
    output = "switchport mode acces % Invalid input detected at '^' marker. ssto145cis(config-if)#"
    assert interactive_prompt_hint(f"{PREFIX}{output} {EXPECTS}") == ""


def test_expected_prompt_list_alone_never_triggers_the_interactive_advice() -> None:
    hint = interactive_prompt_hint(f"{PREFIX}vlan 299 name access {EXPECTS}")
    assert "#INTERACTIVE" not in hint
