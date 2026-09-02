import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
ANCHORS_JS = ROOT / "static" / "assistant_turn_anchors.js"
FEEDBACK_JS = ROOT / "static" / "message_feedback.js"
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
SW_JS = (ROOT / "static" / "sw.js").read_text(encoding="utf-8")


def _run_node(case):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for message feedback frontend tests")
    script = f"""
const fs=require('fs');
const vm=require('vm');
const anchors=fs.readFileSync({json.dumps(str(ANCHORS_JS))},'utf8');
const feedback=fs.readFileSync({json.dumps(str(FEEDBACK_JS))},'utf8');
const sandbox={{console}};
sandbox.window=sandbox;
sandbox.globalThis=sandbox;
sandbox.t=(key)=>key==='feedback_datacop_success'?'DataCop #{{id}}':key;
sandbox.esc=(value)=>String(value);
sandbox.li=(name)=>`<svg data-icon="${{name}}"></svg>`;
sandbox.S={{
  session:{{session_id:'session-one'}},
  messages:[{{role:'assistant',content:'answer',timestamp:12,_feedback:null}}]
}};
sandbox.timers=[];
sandbox.setTimeout=(callback,delay)=>{{ sandbox.timers.push({{callback,delay}}); return sandbox.timers.length; }};
sandbox.clearTimeout=()=>{{}};
sandbox.renderMessages=()=>{{ sandbox.renderCount=(sandbox.renderCount||0)+1; }};
vm.createContext(sandbox);
vm.runInContext(anchors,sandbox,{{filename:'assistant_turn_anchors.js'}});
vm.runInContext(feedback,sandbox,{{filename:'message_feedback.js'}});
(async()=>{{
  let output;
  {case}
  process.stdout.write(JSON.stringify(output));
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    result = subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def test_like_starts_datacop_job_and_polls_to_success():
    result = _run_node(
        """
const calls=[];
sandbox.api=async(path,options)=>{
  calls.push({path,payload:JSON.parse(options.body)});
  return {
    ok:true,
    feedback:'like',
    status:'queued',
    job_id:'job-1',
    session_id:'session-one',
    message_ref:'f'.repeat(64),
  };
};
const buttons=[
  {disabled:false,setAttribute(){},removeAttribute(){}},
  {disabled:false,setAttribute(){},removeAttribute(){}},
];
const row={dataset:{msgIdx:'0'},querySelectorAll:()=>buttons};
const button={closest:()=>row};
await sandbox.toggleMessageFeedback(button,'like');
const anchorRef=calls[0].payload.message_ref;
const canonicalRef='f'.repeat(64);
sandbox.api=async(path)=>{
  calls.push({path,payload:null});
  return {
    ok:true,
    feedback:'like',
    status:'succeeded',
    job_id:'job-1',
    session_id:'session-one',
    message_ref:canonicalRef,
    datacop_problem_id:91,
    error:null,
  };
};
await sandbox.__messageFeedbackTest.pollMessageFeedbackJob(
  'session-one',0,anchorRef,canonicalRef,'job-1'
);
output={
  feedback:sandbox.S.messages[0]._feedback,
  status:sandbox.S.messages[0]._feedback_status,
  problemId:sandbox.S.messages[0]._feedback_datacop_problem_id,
  payloads:calls.map(call=>call.payload),
  paths:calls.map(call=>call.path),
  renderCount:sandbox.renderCount,
};
"""
    )
    assert result["feedback"] == "like"
    assert result["status"] == "succeeded"
    assert result["problemId"] == 91
    assert result["paths"] == ["/api/message-feedback", "/api/message-feedback/jobs/job-1"]
    assert result["payloads"][0]["feedback"] == "like"
    assert result["payloads"][0]["session_id"] == "session-one"
    assert result["payloads"][0]["message_ref"].startswith('{"role":"assistant"')
    assert result["renderCount"] == 2


def test_feedback_request_failure_keeps_unsubmitted_state_and_reenables_controls():
    result = _run_node(
        """
sandbox.api=async()=>{ throw new Error('offline'); };
sandbox.showToast=()=>{ sandbox.toastCount=(sandbox.toastCount||0)+1; };
const controls=[
  {disabled:false,setAttribute(){},removeAttribute(){}},
  {disabled:false,setAttribute(){},removeAttribute(){}},
];
const row={dataset:{msgIdx:'0'},querySelectorAll:()=>controls};
await sandbox.toggleMessageFeedback({closest:()=>row},'like');
output={
  feedback:sandbox.S.messages[0]._feedback,
  renderCount:sandbox.renderCount||0,
  toastCount:sandbox.toastCount||0,
  controlsEnabled:controls.every(control=>control.disabled===false),
};
"""
    )
    assert result == {
        "feedback": None,
        "renderCount": 0,
        "toastCount": 1,
        "controlsEnabled": True,
    }


def test_feedback_buttons_render_only_for_eligible_assistant_answers():
    result = _run_node(
        """
const html=sandbox.messageFeedbackButtonsHtml(
  {
    role:'assistant',content:'answer',timestamp:12,_feedback:'like',
    _feedback_status:'succeeded',_feedback_datacop_problem_id:91,
  },
  true
);
output={
  html,
  hidden:sandbox.messageFeedbackButtonsHtml({role:'assistant',content:'answer'},false),
};
"""
    )
    assert 'data-feedback="like"' in result["html"]
    assert 'data-feedback="dislike"' in result["html"]
    assert 'aria-pressed="true"' in result["html"]
    assert 'disabled' in result["html"]
    assert 'DataCop #91' in result["html"]
    assert result["hidden"] == ""


def test_accepted_feedback_is_immutable_and_dislike_does_not_poll():
    result = _run_node(
        """
const calls=[];
sandbox.S.messages[0]._feedback='dislike';
sandbox.S.messages[0]._feedback_status='received';
sandbox.api=async(path,options)=>{ calls.push({path,options}); throw new Error('must not call'); };
const controls=[
  {disabled:false,setAttribute(){},removeAttribute(){}},
  {disabled:false,setAttribute(){},removeAttribute(){}},
];
const row={dataset:{msgIdx:'0'},querySelectorAll:()=>controls};
await sandbox.toggleMessageFeedback({closest:()=>row},'like');
output={calls:calls.length,timers:sandbox.timers.length,feedback:sandbox.S.messages[0]._feedback};
"""
    )
    assert result == {"calls": 0, "timers": 0, "feedback": "dislike"}


def test_feedback_eligibility_is_limited_to_settled_final_assistant_answers():
    result = _run_node(
        """
const eligible=sandbox.messageFeedbackEligible;
const finalContext={isTurnFinalAssistant:true,isProcessWakeup:false,displayContent:'answer'};
output={
  final:eligible({role:'assistant',content:'answer'},finalContext),
  user:eligible({role:'user',content:'question'},finalContext),
  live:eligible({role:'assistant',content:'answer',_live:true},finalContext),
  error:eligible({role:'assistant',content:'answer',_error:true},finalContext),
  toolOnly:eligible(
    {role:'assistant',content:'working',tool_calls:[{id:'tool-1'}]},
    finalContext
  ),
  textAfterTool:eligible(
    {role:'assistant',content:[
      {type:'tool_use',id:'tool-1'},
      {type:'output_text',text:'answer'},
    ]},
    finalContext
  ),
  intermediate:eligible(
    {role:'assistant',content:'answer'},
    {...finalContext,isTurnFinalAssistant:false}
  ),
  empty:eligible(
    {role:'assistant',content:''},
    {...finalContext,displayContent:'  '}
  ),
};
"""
    )
    assert result == {
        "final": True,
        "user": False,
        "live": False,
        "error": False,
        "toolOnly": False,
        "textAfterTool": True,
        "intermediate": False,
        "empty": False,
    }


def test_feedback_module_is_loaded_before_ui_and_cached_for_pwa():
    feedback_script = 'static/message_feedback.js?v=__WEBUI_VERSION__'
    assert feedback_script in INDEX_HTML
    assert INDEX_HTML.index(feedback_script) < INDEX_HTML.index('static/ui.js?v=__WEBUI_VERSION__')
    assert "'./static/message_feedback.js' + VQ" in SW_JS
    assert '.msg-feedback-btn[aria-pressed="true"]' in STYLE_CSS
    assert '.msg-foot:has(.msg-feedback-status)' in STYLE_CSS
    assert '@media(max-width:640px)' in STYLE_CSS
