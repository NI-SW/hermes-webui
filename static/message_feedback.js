// Three-state feedback for settled assistant answers. Session transcripts own
// message content and context; this module persists only like/dislike state.

function _messageFeedbackValue(value){
  return value==='like'||value==='dislike'?value:null;
}

function _messageFeedbackPartText(part){
  if(part==null) return '';
  if(typeof part==='string') return part;
  if(typeof part!=='object') return String(part);
  return String(part.text||part.content||part.input_text||part.output_text||'');
}

function _messageFeedbackPartIsToolUse(part){
  if(!part||typeof part!=='object') return false;
  const type=String(part.type||'').toLowerCase();
  if(type==='tool_use'||type==='tool_call') return true;
  if(type||_messageFeedbackPartText(part).trim()) return false;
  return ['tool_use_id','tool_call_id','call_id'].some(key=>key in part)
    &&['name','tool_name','input','args'].some(key=>key in part);
}

function _messageFeedbackHasFinalVisibleText(message){
  const content=message&&message.content;
  if(Array.isArray(content)){
    let lastToolIndex=-1;
    content.forEach((part,index)=>{
      if(_messageFeedbackPartIsToolUse(part)) lastToolIndex=index;
    });
    if(lastToolIndex>=0){
      return content.slice(lastToolIndex+1).some(part=>_messageFeedbackPartText(part).trim());
    }
    if(Array.isArray(message.tool_calls)&&message.tool_calls.length) return false;
    return content.some(part=>_messageFeedbackPartText(part).trim());
  }
  if(Array.isArray(message&&message.tool_calls)&&message.tool_calls.length) return false;
  return !!String(content||'').trim();
}

function messageFeedbackEligible(message, context){
  return !!(
    message
    &&message.role==='assistant'
    &&!message._live
    &&!message._error
    &&!message._statusCard
    &&context
    &&context.isTurnFinalAssistant
    &&!context.isProcessWakeup
    &&_messageFeedbackHasFinalVisibleText(message)
    &&String(context.displayContent||'').trim()
  );
}

function messageFeedbackButtonsHtml(message, eligible){
  if(!eligible) return '';
  const current=_messageFeedbackValue(message&&message._feedback);
  const button=(feedback,icon,labelKey)=>{
    const selected=current===feedback;
    return `<button class="msg-action-btn msg-feedback-btn" type="button" data-feedback="${feedback}" aria-pressed="${selected?'true':'false'}" title="${esc(t(labelKey))}" aria-label="${esc(t(labelKey))}" onclick="toggleMessageFeedback(this,'${feedback}')">${li(icon,13)}</button>`;
  };
  return button('like','thumbs-up','feedback_like')+button('dislike','thumbs-down','feedback_dislike');
}

async function toggleMessageFeedback(button, requestedFeedback){
  if(requestedFeedback!=='like'&&requestedFeedback!=='dislike'){
    throw new TypeError('requested feedback must be like or dislike');
  }
  const row=button&&button.closest?button.closest('[data-msg-idx]'):null;
  const rawIdx=row?Number(row.dataset.msgIdx):NaN;
  if(!Number.isSafeInteger(rawIdx)||rawIdx<0) return;
  if(!S.session||!Array.isArray(S.messages)) return;
  const message=S.messages[rawIdx];
  if(!message||message.role!=='assistant'||message._live) return;
  const anchors=window.HermesAssistantTurnAnchors;
  if(!anchors||typeof anchors.assistantTurnMessageRef!=='function'){
    throw new Error('assistant message reference service is unavailable');
  }
  const sessionId=String(S.session.session_id||'');
  if(!sessionId) throw new Error('active session has no session_id');
  const messageRef=anchors.assistantTurnMessageRef(message);
  if(!messageRef) throw new Error('assistant message has no stable reference');
  const nextFeedback=_messageFeedbackValue(message._feedback)===requestedFeedback
    ? null
    : requestedFeedback;
  const controls=row.querySelectorAll('.msg-feedback-btn');
  controls.forEach(control=>{
    control.disabled=true;
    control.setAttribute('aria-busy','true');
  });
  try{
    const response=await api('/api/message-feedback',{
      method:'POST',
      body:JSON.stringify({
        session_id:sessionId,
        message_ref:messageRef,
        feedback:nextFeedback,
      }),
    });
    if(!response||response.ok!==true||response.feedback!==nextFeedback){
      throw new Error('message feedback response did not match the requested state');
    }
    if(!S.session||S.session.session_id!==sessionId) return;
    const current=S.messages[rawIdx];
    if(!current||anchors.assistantTurnMessageRef(current)!==messageRef) return;
    current._feedback=nextFeedback;
    renderMessages({preserveScroll:true});
  }catch(error){
    if(typeof showToast==='function') showToast(`${t('feedback_save_failed')}: ${error.message}`,3600,'error');
  }finally{
    controls.forEach(control=>{
      control.disabled=false;
      control.removeAttribute('aria-busy');
    });
  }
}

if(typeof window!=='undefined'){
  window.__messageFeedbackTest=Object.freeze({
    messageFeedbackEligible,
    messageFeedbackButtonsHtml,
    toggleMessageFeedback,
  });
}
