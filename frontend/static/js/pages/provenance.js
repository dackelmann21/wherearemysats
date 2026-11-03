// Manage exact records and entry origins. Existing Acquisition forms own writes.
window.ProvenancePage = (function () {
  var root=null, active=false, generation=0, controller=null, timer=null, anchorController=null;
  var population='acquisitions', state='', search='', scopeWallet=null, records=[], receipts=null, recordError=null, receiptError=null, receiptController=null, receiptTimer=null;
  var pageIndex=0, pageSize=25, initialPosition=null, targetId=null, acquisitionsLoaded=false, receiptsLoading=false;
  // Dashboard deep links (D-06): review=required (Needs Attention) and type=anchors (all known anchors).
  var reviewFilter=null, anchors=null, anchorError=null;
  function ui(){return window.AcquisitionsPage;}
  function stop(){active=false; ++generation; if(controller)controller.abort(); if(receiptController)receiptController.abort(); if(anchorController)anchorController.abort(); if(receiptTimer!==null)clearTimeout(receiptTimer);receiptTimer=null; if(timer!==null)clearTimeout(timer); timer=null;}
  function headerHtml(){return '<h1>Manage Records &amp; Origins</h1>';}
  function filtersHtml(){return '<div class="prov-filter-row"><button class="btn" id="records-acquisition">Acquisition</button>'+
    '<button class="btn" id="records-no-acquisition">No-Acquisition</button>'+
    '<label>Find <input id="records-search" type="search" placeholder="#ID, source, reference, address or output"></label>'+
    '<button class="btn" id="records-reload">Reload</button></div>'+
    '<div id="receipts-filter-bar" hidden><label>Review <select id="receipts-state"><option value="">All</option><option value="UNRESOLVED">Open</option><option value="NO_ACQUISITION">No-Acquisition confirmed</option></select></label></div>'+
    '<p id="records-status" class="hint" aria-live="polite"></p>';}
  function advancedHtml(){return '';}
  function setPopulation(value){population=value; pageIndex=0; ui().closeEditors(root); renderTable(); if(value==='no-acquisition'&&receipts===null&&!receiptsLoading)loadReceipts();}
  function wire(el){
    el.querySelector('#records-acquisition').addEventListener('click',function(){setPopulation('acquisitions');});
    el.querySelector('#records-no-acquisition').addEventListener('click',function(){setPopulation('no-acquisition');});
    el.querySelector('#records-search').addEventListener('input',function(){search=this.value.trim().toLowerCase();pageIndex=0;ui().closeEditors(root);renderTable();});
    el.querySelector('#receipts-state').addEventListener('change',function(){state=this.value;pageIndex=0;renderTable();});
    el.querySelector('#records-reload').addEventListener('click',reload);
  }
  // FIX-UI-4: "#n" and "n" match Acquisition ID n exactly (#15 does not match 150); text fields keep substring search.
  function matches(r){var id=/^#?([1-9][0-9]*)$/.exec(search);if(id&&r.id!=null&&String(r.id)===id[1])return true;
    return !search || [r.source,r.reference,r.notes,r.origin_txid,r.origin_address,r.address,r.outpoint,r.wallet_name].some(function(v){return String(v==null?'':v).toLowerCase().indexOf(search)>=0;});}
  function wireOutputs(el){el.querySelectorAll('[data-review-output]').forEach(function(b){b.addEventListener('click',function(){var p=b.dataset.reviewOutput.split(':');App.openOutpoint(p[0],Number(p[1]));});});}
  function receiptRow(r){var confirmed=r.origin_resolution.type==='NO_ACQUISITION';return '<div class="acq-row receipt-row" data-receipt="'+Fmt.esc(r.outpoint)+'">'+
    '<div>'+Fmt.esc(r.block_time?new Date(r.block_time*1000).toISOString().slice(0,10):'—')+'</div>'+
    '<div>'+Fmt.esc(r.wallet_name||'—')+'</div><div>'+Fmt.btc(r.amount_sats)+' BTC</div>'+
    '<div class="mono">'+Fmt.esc(r.outpoint)+'<br>'+Fmt.esc(r.address||'—')+'</div>'+
    '<div>'+Fmt.esc(r.spend_status||'UNKNOWN')+'</div><div>'+(confirmed?'No-Acquisition confirmed':'Open origin decision')+
    (r.review_status==='UNRESOLVED'&&confirmed?'<br>Evidence review open':'')+'</div>'+
    '<div><button class="btn small" data-review-output="'+Fmt.esc(r.outpoint)+'">Review origin / ownership</button></div></div>';}
  function receiptTable(content){return '<div class="acq-list-wrap"><div class="receipt-grid-table"><div class="acq-table-head"><span>Date</span><span>Wallet</span><span>Received amount</span><span>Output / address</span><span>Spend state</span><span>Origin decision</span><span>Actions</span></div>'+content+'</div></div>';}
  function anchorRow(r){return '<div class="acq-row receipt-row" data-anchor="'+Fmt.esc(r.outpoint)+'">'+
    '<div class="mono">'+Fmt.esc(r.outpoint)+'</div><div>'+Fmt.esc(r.source||'—')+'</div>'+
    '<div><button class="btn small" data-review-output="'+Fmt.esc(r.outpoint)+'">Review origin / ownership</button></div></div>';}
  function anchorTable(content){return '<div class="acq-list-wrap"><div class="receipt-grid-table"><div class="acq-table-head"><span>Anchor output</span><span>Source</span><span>Actions</span></div>'+content+'</div></div>';}
  function renderAnchors(){
    root.querySelector('#records-status').textContent=anchorError?anchorError:anchors===null?'Loading No-Acquisition anchors…':anchors.length+' known No-Acquisition anchors in scope · saved and derived, including historical anchors.';
    var selected=(anchors||[]).filter(matches), pages=Math.max(1,Math.ceil(selected.length/pageSize));pageIndex=Math.min(pageIndex,pages-1);
    var content=anchorError?'<p class="alert warn">'+Fmt.esc(anchorError)+'</p>':selected.slice(pageIndex*pageSize,(pageIndex+1)*pageSize).map(anchorRow).join('');
    if(!content)content='<p class="hint">'+(anchors===null?'Loading…':'No matching records.')+'</p>';
    root.querySelector('#acq-list').innerHTML=anchorTable(content);
    wireOutputs(root.querySelector('#acq-list'));
    if(pages>1){var nav=document.createElement('nav');nav.className='prov-pagination';nav.innerHTML='<button class="btn small" id="records-prev"'+(!pageIndex?' disabled':'')+'>Previous</button><span>Page '+(pageIndex+1)+' of '+pages+'</span><button class="btn small" id="records-next"'+(pageIndex===pages-1?' disabled':'')+'>Next</button>';root.querySelector('#acq-list').appendChild(nav);['prev','next'].forEach(function(d){root.querySelector('#records-'+d).addEventListener('click',function(){pageIndex+=d==='next'?1:-1;renderAnchors();});});}
  }
  function renderTable(){
    if(!active||!root)return;
    if(population==='anchors'){root.querySelector('#records-acquisition').setAttribute('aria-pressed','false');root.querySelector('#records-no-acquisition').setAttribute('aria-pressed','true');
      root.querySelector('#acq-filter-bar').hidden=true;root.querySelector('#receipts-filter-bar').hidden=true;renderAnchors();return;}
    var isAcquisition=population==='acquisitions', error=isAcquisition?recordError:receiptError;
    root.querySelector('#records-acquisition').setAttribute('aria-pressed',String(isAcquisition));
    root.querySelector('#records-no-acquisition').setAttribute('aria-pressed',String(!isAcquisition));
    root.querySelector('#acq-filter-bar').hidden=!isAcquisition;
    root.querySelector('#receipts-filter-bar').hidden=isAcquisition;
    var selected=(isAcquisition?records:(receipts||[])).filter(matches).filter(function(r){return isAcquisition || !state || r.origin_resolution.type===state;});
    if(isAcquisition&&targetId){var targetIndex=selected.findIndex(function(r){return String(r.id)===targetId;});if(targetIndex>=0)pageIndex=Math.floor(targetIndex/pageSize);}
    var pages=Math.max(1,Math.ceil(selected.length/pageSize));pageIndex=Math.min(pageIndex,pages-1);
    root.querySelector('#records-status').textContent=error?error:((isAcquisition&&!acquisitionsLoaded)?'Loading Acquisition records…':(!isAcquisition&&receipts===null)?'Loading saved wallet entries…':selected.length+' '+(isAcquisition?(reviewFilter?'Acquisition records requiring review (position or wallet membership)':'Acquisition records'):'wallet entry origins · Saved receipts, including spent entries. Refresh Wallet Inventory for new receipts.'));
    var content=error?'<p class="alert warn">'+Fmt.esc(error)+'</p>':selected.slice(pageIndex*pageSize,(pageIndex+1)*pageSize).map(function(r){return isAcquisition?ui().recordRowHtml(r):receiptRow(r);}).join('');
    if(!content)content='<p class="hint">'+((isAcquisition&&!acquisitionsLoaded)||(!isAcquisition&&receipts===null)?'Loading…':'No matching records.')+'</p>';
    root.querySelector('#acq-list').innerHTML=isAcquisition?ui().tableHtml(content):receiptTable(content);
    if(isAcquisition)ui().wireRows(root,records); if(isAcquisition&&targetId){var target=root.querySelector('.acq-row[data-id="'+targetId+'"]');if(target){target.setAttribute('tabindex','-1');target.focus();target.scrollIntoView({block:'center'});targetId=null;}}
    wireOutputs(root.querySelector('#acq-list'));
    if(pages>1){var nav=document.createElement('nav');nav.className='prov-pagination';nav.innerHTML='<button class="btn small" id="records-prev"'+(!pageIndex?' disabled':'')+'>Previous</button><span>Page '+(pageIndex+1)+' of '+pages+'</span><button class="btn small" id="records-next"'+(pageIndex===pages-1?' disabled':'')+'>Next</button>';root.querySelector('#acq-list').appendChild(nav);['prev','next'].forEach(function(d){root.querySelector('#records-'+d).addEventListener('click',function(){ui().closeEditors(root);pageIndex+=d==='next'?1:-1;renderTable();});});}
  }
  function loadReceipts(){
    receiptsLoading=true;receiptController=new AbortController(); var request=generation, collected=[], first=null;receiptError=null;renderTable();
    receiptTimer=setTimeout(function(){if(active&&request===generation){receiptController.abort();receiptError='Wallet entry request timed out. Reload to retry.';receiptsLoading=false;renderTable();}},90000);
    function page(offset){API.walletReceipts(offset,1000,receiptController.signal,scopeWallet).then(function(r){
      if(!active||request!==generation)return;
      if(!first)first=r;
      if(r.snapshot_id!==first.snapshot_id||r.total!==first.total||!Array.isArray(r.records)||(!r.records.length&&collected.length<r.total)){throw Error('Wallet entry evidence changed. Reload to retry.');}
      collected=collected.concat(r.records);
      if(collected.length<r.total){page(collected.length);return;}
      if(collected.length!==r.total||new Set(collected.map(function(v){return v.outpoint;})).size!==r.total){throw Error('Incomplete wallet entry evidence. Reload to retry.');}
      clearTimeout(receiptTimer);receiptTimer=null;receipts=collected;receiptsLoading=false;renderTable();
    }).catch(function(e){if(active&&request===generation){clearTimeout(receiptTimer);receiptTimer=null;receiptsLoading=false;receiptError=e.message||'Wallet entries unavailable. Reload to retry.';renderTable();}});}
    page(0);
  }
  function loadAnchors(){
    anchorController=new AbortController(); var request=generation, collected=[], first=null; anchorError=null; anchors=null; renderTable();
    function page(offset){API.noAcquisitionAnchors(offset,100,anchorController.signal,scopeWallet).then(function(r){
      if(!active||request!==generation)return;
      if(!first)first=r;
      if(r.snapshot_id!==first.snapshot_id||r.total!==first.total||!Array.isArray(r.records)||(!r.records.length&&collected.length<r.total)){throw Error('No-Acquisition anchors changed. Reload to retry.');}
      collected=collected.concat(r.records);
      if(collected.length<r.total){page(collected.length);return;}
      anchors=collected;renderTable();
    }).catch(function(e){if(active&&request===generation){anchorError=e.message||'No-Acquisition anchors unavailable. Reload to retry.';renderTable();}});}
    page(0);
  }
  function load(el,filter){
    stop();active=true;root=el;var request=generation;controller=new AbortController();recordError=null;receiptError=null;receipts=null;receiptsLoading=false;acquisitionsLoaded=false;records=[];
    ui().closeEditors(root);renderTable();
    timer=setTimeout(function(){if(active&&request===generation){controller.abort();recordError='Acquisition request timed out. Reload to retry.';renderTable();}},90000);
    API.listAcquisitions(filter,controller.signal,true,scopeWallet,reviewFilter).then(function(a){
      if(!active||request!==generation)return;clearTimeout(timer);timer=null;records=a;acquisitionsLoaded=true;ui().membership(root,{acquisition_wallet_positions:a.map(function(r){return {acquisition_id:r.id,state:(r.wallet_position||{}).state};})},'ready');renderTable();
      
    }).catch(function(e){if(active&&request===generation){clearTimeout(timer);timer=null;recordError=e.message||'Records unavailable. Reload to retry.';renderTable();}});
    if(population==='no-acquisition')loadReceipts();
    if(population==='anchors')loadAnchors();
  }
  function filter(el,key){population='acquisitions';reviewFilter=null;pageIndex=0;load(el,key);}
  function render(el,params,requested){stop();root=el;active=true;population='acquisitions';state='';search='';pageIndex=0;params=params||{get:function(){return null;}};scopeWallet=params.get('wallet');initialPosition=params.get('position');reviewFilter=params.get('review');anchors=null;anchorError=null;targetId=params.get('acquisition_id');if(targetId&&!/^[1-9][0-9]*$/.test(targetId))targetId=null;if(requested==='origins'||params.get('type')==='no-acquisition')population='no-acquisition';if(params.get('type')==='anchors')population='anchors';ui().render(el,workspace);}
  function reload(){if(active&&root)ui().refresh(root);}
  var workspace={render:render,stop:stop,reload:reload,load:load,filter:filter,headerHtml:headerHtml,filtersHtml:filtersHtml,advancedHtml:advancedHtml,wire:wire,acquisitionView:function(){return population==='acquisitions';},walletScope:function(){return scopeWallet;},initialFilter:function(){return initialPosition;}};
  return workspace;
})();