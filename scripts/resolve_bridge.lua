-- Native Resolve bridge. Load with dofile(), then call snapshot(resolve,path) or apply(resolve,config).
local M = {}
local function quote(s)
 return '"'..tostring(s):gsub('[%z\1-\31\\"]',function(c)
  if c=='\\' then return '\\\\' end
  if c=='"' then return '\\"' end
  return string.format('\\u%04x',string.byte(c))
 end)..'"'
end
local function json(v)
 if v==nil then return 'null' end
 if type(v)=='boolean' or type(v)=='number' then return tostring(v) end
 if type(v)~='table' then return quote(v) end
 local a={}
 if #v>0 then for _,x in ipairs(v) do a[#a+1]=json(x) end;return '['..table.concat(a,',')..']' end
 local keys={};for k in pairs(v) do keys[#keys+1]=k end;table.sort(keys)
 for _,k in ipairs(keys) do a[#a+1]=quote(k)..':'..json(v[k]) end
 return '{'..table.concat(a,',')..'}'
end
local function save(path,value)
 local f=assert(io.open(path,'w'));f:write(json(value));f:close()
end
local function exists(path)
 local f=io.open(path,'rb');if f then f:close();return true end;return false
end
local function optional(fn)
 local ok,v=pcall(fn);if ok then return v end;return nil
end
local function collect(r)
 assert(r,'Resolve object unavailable; try the console global resolve or fu:GetResolve()')
 local p=assert(r:GetProjectManager():GetCurrentProject(),'No active project')
 local tl=assert(p:GetCurrentTimeline(),'Create/select an empty timeline first if the project has no timeline')
 local data={name=p:GetName(),version=r:GetVersionString(),settings=p:GetSetting(),timelines={},media={},
  timeline={name=tl:GetName(),start=tl:GetStartFrame(),finish=tl:GetEndFrame(),settings=tl:GetSetting(),tracks={}}}
 for i=1,p:GetTimelineCount() do data.timelines[#data.timelines+1]={name=p:GetTimelineByIndex(i):GetName()} end
 local function folder(f)
  for _,m in ipairs(f:GetClipList() or {}) do data.media[#data.media+1]={name=m:GetName(),id=m:GetMediaId(),props=m:GetClipProperty()} end
  for _,c in ipairs(f:GetSubFolderList() or {}) do folder(c) end
 end
 folder(p:GetMediaPool():GetRootFolder())
 for _,kind in ipairs({'video','audio'}) do for i=1,tl:GetTrackCount(kind) do
  local t={type=kind,index=i,name=tl:GetTrackName(kind,i),enabled=tl:GetIsTrackEnabled(kind,i),items={}}
  for _,it in ipairs(tl:GetItemListInTrack(kind,i) or {}) do
   local media=it:GetMediaPoolItem()
   t.items[#t.items+1]={name=it:GetName(),start=it:GetStart(),finish=it:GetEnd(),duration=it:GetDuration(),
    left=it:GetLeftOffset(),left_precise=optional(function()return it:GetLeftOffset(true) end),
    props=it:GetProperty(),media=media and media:GetMediaId(),enabled=it:GetClipEnabled()}
  end
  data.timeline.tracks[#data.timeline.tracks+1]=t
 end end
 return data
end

function M.snapshot(r,path)
 local value=collect(r);save(path,value);print('SNAPSHOT SAVED',path);return value
end

local function compare_baseline(actual,expected)
 assert(actual.name==expected.name and actual.timeline.name==expected.timeline.name,'Active project/timeline changed since snapshot')
 assert(actual.timeline.start==expected.timeline.start,'Timeline start changed')
 assert(#actual.timeline.tracks==#expected.timeline.tracks,'Track layout changed')
 for i,t in ipairs(expected.timeline.tracks) do
  local a=actual.timeline.tracks[i]
  assert(a.type==t.type and a.index==t.index and a.enabled==t.enabled,'Track state changed')
  assert(#a.items==#t.items,'Original item count changed')
  for j,it in ipairs(t.items) do
   local x=a.items[j]
   assert(x.media==it.media and x.start==it.start and x.finish==it.finish and x.left==it.left and x.enabled==it.enabled,'Original clip changed')
  end
 end
end

function M.apply(r,config_path)
 local c=assert(dofile(config_path));assert(c.schema==1,'Unsupported plan schema')
 local pm=r:GetProjectManager();local p=assert(pm:GetCurrentProject());local pool=p:GetMediaPool()
 assert(p:GetName()==c.project_name,'Wrong project')
 local original=assert(p:GetCurrentTimeline());assert(original:GetName()==c.original_name,'Select the baseline timeline before applying')
 compare_baseline(collect(r),c.baseline)
 for i=1,p:GetTimelineCount() do assert(p:GetTimelineByIndex(i):GetName()~=c.result_name,'Result timeline already exists; inspect the previous run') end
 for _,path in ipairs({c.export_path,c.backup_path,c.log_path,c.after_snapshot}) do assert(not exists(path),'Output already exists; inspect the previous run: '..path) end
 local media={}
 local function folder(f)
  for _,m in ipairs(f:GetClipList() or {}) do media[m:GetMediaId()]=m end
  for _,x in ipairs(f:GetSubFolderList() or {}) do folder(x) end
 end
 folder(pool:GetRootFolder())
 local origin=original:GetStartFrame()
 for _,s in ipairs(c.segments) do
  assert(media[s.media_id],'Source media missing: '..s.name)
  assert(media[s.media_id]:GetClipProperty()['File Path']==s.path,'Source path changed: '..s.name)
  for _,dest in ipairs({{'video',s.video_track},{'audio',s.audio_track}}) do
   if dest[2]<=original:GetTrackCount(dest[1]) then
    for _,it in ipairs(original:GetItemListInTrack(dest[1],dest[2]) or {}) do
     assert(it:GetEnd()<=origin+s.start_frame or it:GetStart()>=origin+s.end_frame,'Planned clip overlaps existing work')
    end
   end
  end
 end
 assert(pm:ExportProject(p:GetName(),c.backup_path,false),'Project backup failed')
 local log=assert(io.open(c.log_path,'w'))
 local tl
 local ok,err=xpcall(function()
  tl=assert(original:DuplicateTimeline(c.result_name),'Could not duplicate timeline')
  assert(p:SetCurrentTimeline(tl))
  local maxv=math.max(unpack(c.video_tracks));local maxa=math.max(c.master.master_track,unpack(c.audio_tracks))
  while tl:GetTrackCount('video')<maxv do assert(tl:AddTrack('video')) end
  while tl:GetTrackCount('audio')<maxa do assert(tl:AddTrack('audio','stereo')) end
  local function append(media_id,source_frame,source_span,source_fps,source_sec,a,b,kind,index,name)
   local span=source_span
   for attempt=1,3 do
    local arr=pool:AppendToTimeline({{mediaPoolItem=assert(media[media_id]),startFrame=source_frame,
     endFrame=source_frame+span,mediaType=kind,trackIndex=index,recordFrame=origin+a}})
    local it=arr and arr[1];assert(it and it:GetStart(),'Append failed: '..name)
    local duration=it:GetDuration()
    if duration==b-a then
     assert(it:GetStart()==origin+a and it:GetEnd()==origin+b,'Record position mismatch')
     local left=optional(function()return it:GetLeftOffset(true) end) or it:GetLeftOffset()
     assert(math.abs(left/c.timeline_fps-source_sec)<=2/c.timeline_fps,'Source in-point mismatch: '..name)
     log:write(name,' mediaType=',kind,' track=',index,' start=',it:GetStart(),' end=',it:GetEnd(),' sourceSeconds=',left/c.timeline_fps,'\n');log:flush()
     return it
    end
    assert(tl:DeleteClips({it},false));span=span+(b-a-duration+1e-6)*source_fps/c.timeline_fps
   end
   error('Cannot achieve intended duration after bounded correction: '..name)
  end
  if not c.master.already_placed then
   local n=c.expected_frames
   append(c.master.id,0,n*c.master.fps/c.timeline_fps+1e-7,c.master.fps,0,0,n,2,c.master.master_track,c.master.name)
  end
  local previous=nil
  for _,s in ipairs(c.segments) do
   local v=append(s.media_id,s.source_frame,s.source_span,s.source_fps,s.source_sec,s.start_frame,s.end_frame,1,s.video_track,s.name)
   local a=append(s.media_id,s.source_frame,s.source_span,s.source_fps,s.source_sec,s.start_frame,s.end_frame,2,s.audio_track,s.name)
   assert(tl:SetClipsLinked({v,a},true),'Could not link video/audio')
   if previous~=s.media_id then tl:AddMarker(s.start_frame,'Blue',s.name,'Waveform matched source video; reference audio A'..s.audio_track,1,'waveform-align-'..s.start_frame) end
   previous=s.media_id
  end
  for _,i in ipairs(c.mute_tracks) do if i<=tl:GetTrackCount('audio') then assert(tl:SetTrackEnable('audio',i,false)) end end
  assert(tl:SetTrackEnable('audio',c.master.master_track,true))
  M.snapshot(r,c.after_snapshot)
  assert(pm:SaveProject(),'Project save failed')
  assert(pm:ExportProject(p:GetName(),c.export_path,false),'Project export failed')
  log:write('SUCCESS\n');log:flush()
 end,debug.traceback)
 if not ok then log:write('FAILED: ',tostring(err),'\n');log:flush() end
 log:close()
 if not ok then error(err) end
 print('ALIGNMENT COMPLETE',#c.segments,c.result_name,c.export_path)
 return tl
end

return M
