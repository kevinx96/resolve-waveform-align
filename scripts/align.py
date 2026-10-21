"""Prepare linked media, propose waveform alignment, compile a Resolve plan, verify it.

Analysis and compilation do not mutate Resolve or source media. See --help.
"""
from __future__ import annotations
import os
os.environ.setdefault('OPENBLAS_NUM_THREADS', '4')
os.environ.setdefault('OMP_NUM_THREADS', '4')
import argparse
import hashlib
import json
import math
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
import numpy as np
from scipy import fft, signal
from scipy.io import wavfile

SR = 8000
WAVE_SR = 2000
HOP = 400


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def write(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')


def array(value):
    return value if isinstance(value, list) else []


def rate(value):
    if isinstance(value, str) and '/' in value:
        a, b = value.split('/')
        value = float(a) / float(b)
    value = float(value)
    for nominal in (23.976, 29.97, 47.952, 59.94, 119.88):
        if abs(value - nominal) < .0001:
            return round(nominal * 1001 / 1000) * 1000 / 1001
    if not math.isfinite(value) or value <= 0:
        raise ValueError('Invalid frame rate')
    return value


def track(snapshot, kind, index):
    return next((t for t in snapshot['timeline']['tracks']
                 if t['type'] == kind and t['index'] == index),
                {'type': kind, 'index': index, 'items': []})


def probe(path):
    return json.loads(subprocess.check_output(
        ['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(path)],
        encoding='utf-8'))


def decode(path, cache):
    path = Path(path).resolve()
    stat = path.stat()
    key = hashlib.sha256(f'{path}|{stat.st_size}|{stat.st_mtime_ns}|{SR}'.encode()).hexdigest()[:20]
    dest = cache / f'{key}.wav'
    if not dest.exists():
        subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-y', '-i', str(path),
                        '-vn', '-ac', '1', '-ar', str(SR), '-c:a', 'pcm_s16le', str(dest)], check=True)
    return dest


def audio(path):
    sr, x = wavfile.read(path)
    if sr != SR or x.ndim != 1:
        raise ValueError(f'Expected {SR} Hz mono audio: {path}')
    return x.astype(np.float32) / 32768


def prepare(args):
    for executable in ('ffmpeg', 'ffprobe'):
        if not shutil.which(executable):
            raise ValueError(f'{executable} is not available')
    snapshot = read(args.snapshot)
    out = Path(args.workdir).resolve()
    cache = out / 'audio'
    cache.mkdir(parents=True, exist_ok=True)
    fps = rate(snapshot['timeline']['settings']['timelineFrameRate'])
    media = snapshot['media']
    targets = [m for m in media if m['id'] == args.target or m['props'].get('File Path') == args.target]
    if len(targets) != 1:
        raise ValueError('Select exactly one target media ID or exact File Path from the snapshot')
    target = targets[0]
    candidate_ids = set(args.source_id or [])
    sources = []
    for m in media:
        props = m['props']
        path = props.get('File Path')
        if m['id'] == target['id'] or not path:
            continue
        if candidate_ids and m['id'] not in candidate_ids:
            continue
        if not props.get('Video Codec'):
            continue
        p = probe(path)
        if not any(s['codec_type'] == 'audio' for s in p['streams']):
            continue
        video = next(s for s in p['streams'] if s['codec_type'] == 'video')
        sources.append({'id': m['id'], 'name': m['name'], 'path': path,
                        'fps': rate(props.get('FPS') or video['avg_frame_rate']),
                        'duration': float(video.get('duration') or p['format']['duration']),
                        'wav': str(decode(path, cache))})
        print('SOURCE', m['name'], flush=True)
    if not sources:
        raise ValueError('No linked source videos with audio were selected')
    if candidate_ids - {s['id'] for s in sources}:
        raise ValueError('Some --source-id values do not identify readable videos with audio')
    target_path = target['props']['File Path']
    target_wav = decode(target_path, cache)
    raw = audio(target_wav)
    items = array(track(snapshot, 'audio', args.master_track)['items'])
    if any(x['media'] != target['id'] for x in items):
        raise ValueError('The selected master track contains other media; inspect its layout first')
    if items:
        origin = snapshot['timeline']['start']
        end = max(x['finish'] for x in items)
        length = round((end - origin) / fps * SR)
        mixed = np.zeros(length, dtype=np.float32)
        previous_end = 0
        for it in sorted(items, key=lambda x: x['start']):
            a = round((it['start'] - origin) / fps * SR)
            b = round((it['finish'] - origin) / fps * SR)
            if a < 0 or b <= a:
                raise ValueError('Invalid master clip extent')
            if a < previous_end:
                raise ValueError('Overlapping master clips need a rendered analysis reference')
            previous_end = b
            if not it.get('enabled', True):
                continue
            left = it.get('left_precise', it['left'])
            source = round(left / fps * SR)
            if source < 0 or source + b - a > len(raw) + math.ceil(SR / fps):
                raise ValueError('Master extent exceeds the target file; check retiming or source offsets')
            n = max(0, min(b - a, len(raw) - source))
            mixed[a:a+n] = raw[source:source+n]
        target_wav = out / 'target_on_timeline.wav'
        wavfile.write(target_wav, SR, np.clip(mixed * 32768, -32768, 32767).astype(np.int16))
        target_frames = end - origin
    else:
        target_frames = math.floor(len(raw) / SR * fps)
    manifest = {'snapshot': str(Path(args.snapshot).resolve()), 'timeline_fps': fps,
                'target': {'id': target['id'], 'name': target['name'], 'path': target_path,
                           'fps': rate(target['props'].get('FPS') or fps), 'wav': str(target_wav),
                           'duration': len(audio(target_wav)) / SR, 'frames': target_frames,
                           'master_track': args.master_track, 'already_placed': bool(items)},
                'sources': sources}
    write(out / 'manifest.json', manifest)
    print('MANIFEST', out / 'manifest.json', flush=True)


def features(x):
    _, _, z = signal.stft(x, fs=SR, nperseg=1024, noverlap=1024-HOP, boundary='zeros')
    bins = np.fft.rfftfreq(1024, 1 / SR)
    power = np.abs(z) ** 2
    edges = np.geomspace(75, 3800, 49)
    spec = np.log(np.array([power[(bins >= a) & (bins < b)].mean(axis=0)
                            for a, b in zip(edges[:-1], edges[1:])]) + 1e-9)
    spec -= spec.mean(axis=0)
    spec /= np.maximum(spec.std(axis=0), .1)
    idx = np.clip(np.arange(spec.shape[1])[:, None] + [-8, -4, 0, 4, 8], 0, spec.shape[1]-1)
    desc = spec.T[idx].reshape(spec.shape[1], -1)
    desc /= np.maximum(np.linalg.norm(desc, axis=1, keepdims=True), .01)
    return desc.astype(np.float32)


def correlate_data(x, length):
    x = np.asarray(x, dtype=np.float64)
    n = fft.next_fast_len(len(x) + length - 1)
    cs = np.r_[0, np.cumsum(x * x, dtype=np.float64)]
    sums = np.r_[0, np.cumsum(x, dtype=np.float64)]
    energy = cs[length:] - cs[:-length] - (sums[length:] - sums[:-length])**2 / length
    return fft.rfft(x, n), n, np.sqrt(np.maximum(energy, 1e-12)), energy > 1e-6


def match(args):
    manifest = read(args.manifest)
    sources = manifest['sources']
    x8 = [audio(s['wav']) for s in sources]
    target8 = audio(manifest['target']['wav'])
    if len(target8) < args.window * SR:
        raise ValueError('The target is shorter than the analysis window; reduce --window')
    ds, ids = [], []
    for i, x in enumerate(x8):
        d = features(x)[::2]
        ds.append(d)
        ids.extend([i] * len(d))
    database = np.concatenate(ds)
    ids = np.asarray(ids)
    query = features(target8)
    target = signal.resample_poly(target8, 1, 4)
    waves = [signal.resample_poly(x, 1, 4) for x in x8]
    length = round(args.window * WAVE_SR)
    lookup = [correlate_data(x, length) if len(x) >= length else None for x in waves]
    times = np.arange(0, (len(target)-length) / WAVE_SR + 1e-9, args.step)
    centers = times + args.window / 2
    qi = np.clip(np.rint(centers / .05).astype(int), 0, len(query)-1)
    coarse = []
    for k in range(0, len(qi), 32):
        score = query[qi[k:k+32]] @ database.T
        coarse.extend(ids[np.argmax(score, axis=1)].tolist())
    points = []
    previous = None
    for k, (t, center, sid) in enumerate(zip(times, centers, coarse)):
        if lookup[sid] is None:
            continue
        sample = round(t * WAVE_SR)
        q = target[sample:sample+length].astype(np.float64)
        q -= q.mean()
        norm = np.linalg.norm(q)
        if norm < 1e-5:
            continue
        spec, n, denom, valid = lookup[sid]
        corr = fft.irfft(spec * fft.rfft(q[::-1], n), n)[length-1:len(waves[sid])]
        corr /= denom * norm + 1e-15
        corr[~valid] = -1
        np.clip(corr, -1, 1, out=corr)
        best = int(np.argmax(corr))
        peaks, _ = signal.find_peaks(corr, distance=WAVE_SR)
        alternatives = sorted(peaks, key=lambda p: corr[p], reverse=True)[:3]
        if previous is not None and previous['source'] == sid:
            predicted = round((t + previous['offset']) * WAVE_SR)
            a, b = max(0, predicted-150), min(len(corr), predicted+151)
            if b > a:
                local = a + int(np.argmax(corr[a:b]))
                if corr[local] >= corr[best] - .025:
                    best = local
        point = {'time': float(center), 'source': int(sid),
                 'offset': best / WAVE_SR - float(t), 'score': float(corr[best]),
                 'alternatives': [[int(p) / WAVE_SR - float(t), float(corr[p])] for p in alternatives]}
        points.append(point)
        previous = point if point['score'] >= args.min_score else None
        if k % 200 == 0:
            print('MATCH', round(float(center), 1), '/', round(len(target)/WAVE_SR, 1), flush=True)
    make_plan(points, waves, target, sources, args)


def boundary_error(y, x):
    kernel = np.ones(max(3, round(.08*WAVE_SR)))
    xy = signal.fftconvolve(x*y, kernel, mode='same')
    xx = signal.fftconvolve(x*x, kernel, mode='same')
    yy = signal.fftconvolve(y*y, kernel, mode='same')
    return np.maximum(yy-np.maximum(xy, 0)**2/np.maximum(xx, 1e-10), 0) / np.maximum(yy, 1e-6)


def boundary_offset(source, target, offset, a, b):
    q = target[a:b].astype(np.float64)
    start = a+round(offset*WAVE_SR)
    radius = round(.02*WAVE_SR)
    if start-radius < 0 or start+len(q)+radius > len(source) or np.linalg.norm(q) < .001:
        return offset
    x = source[start-radius:start+len(q)+radius].astype(np.float64)
    q -= q.mean()
    cs = np.r_[0, np.cumsum(x*x)]
    sums = np.r_[0, np.cumsum(x)]
    energy = cs[len(q):]-cs[:-len(q)]-(sums[len(q):]-sums[:-len(q)])**2/len(q)
    corr = signal.correlate(x, q, mode='valid') / (np.sqrt(np.maximum(energy, 1e-10))*np.linalg.norm(q)+1e-12)
    best = int(np.argmax(corr))
    return offset+(best-radius)/WAVE_SR if corr[best] >= .4 else offset


def make_plan(points, waves, target, sources, args):
    runs = []
    for p in points:
        if (runs and runs[-1]['source'] == p['source']
                and abs(np.median([x['offset'] for x in runs[-1]['points']])-p['offset']) <= .075
                and p['time']-runs[-1]['points'][-1]['time'] < args.step*1.5):
            runs[-1]['points'].append(p)
        else:
            runs.append({'source': p['source'], 'points': [p]})
    stable = [r for r in runs if len(r['points'])*args.step >= args.min_segment
              and np.median([p['score'] for p in r['points']]) >= args.min_score]
    merged = []
    unmatched = []
    for l, r in zip(stable[:-1], stable[1:]):
        left, right = l['points'][-1]['time'], r['points'][0]['time']
        if right-left > args.window + args.min_segment:
            unmatched.append({'start': left, 'end': right, 'reason': 'No stable match across this interval'})
    for r in stable:
        r['offset'] = float(np.median([p['offset'] for p in r['points']]))
        if merged and merged[-1]['source'] == r['source'] and abs(merged[-1]['offset']-r['offset']) <= .075:
            merged[-1]['points'].extend(r['points'])
            merged[-1]['offset'] = float(np.median([p['offset'] for p in merged[-1]['points']]))
        else:
            merged.append(r)
    if not merged:
        raise ValueError('No stable matching sections; check candidate versions, tempo and pitch')
    duration = len(target) / WAVE_SR
    review = unmatched
    if merged[0]['points'][0]['time'] > args.window/2 + args.min_segment:
        review.append({'start': 0, 'end': merged[0]['points'][0]['time'], 'reason': 'Unmatched leading range'})
    if duration-merged[-1]['points'][-1]['time'] > args.window/2 + args.min_segment:
        review.append({'start': merged[-1]['points'][-1]['time'], 'end': duration, 'reason': 'Unmatched trailing range'})
    boundaries = [0.0]
    for l, r in zip(merged[:-1], merged[1:]):
        left = l['points'][-1]['time']
        right = r['points'][0]['time']
        center = (left + right) / 2
        radius = min(1.0, (center-boundaries[-1])*.45)
        a = max(0, round((center-radius)*WAVE_SR))
        b = min(len(target), round((center+radius)*WAVE_SR))
        y = target[a:b].astype(np.float64)
        estimates = []
        pad = max(1, (b-a)//4)
        for j, item in enumerate((l, r)):
            offset = boundary_offset(waves[item['source']], target, item['offset'],
                                     a if j == 0 else b-pad, a+pad if j == 0 else b)
            idx = np.arange(a, b) + round(offset*WAVE_SR)
            valid = (idx >= 0) & (idx < len(waves[item['source']]))
            x = np.zeros(b-a)
            x[valid] = waves[item['source']][idx[valid]]
            estimates.append(x)
        x1, x2 = estimates
        cost = np.r_[0, np.cumsum(boundary_error(y, x1)-boundary_error(y, x2))]
        low, high = max(1, int(len(y)*.2)), max(2, int(len(y)*.8))
        cut = (a+low+int(np.argmin(cost[low:high]))) / WAVE_SR
        boundaries.append(cut)
    boundaries.append(duration)
    segments = []
    for i, r in enumerate(merged):
        source = sources[r['source']]
        a, b = boundaries[i:i+2]
        confidence = float(np.median([p['score'] for p in r['points']]))
        segments.append({'media_id': source['id'], 'name': source['name'],
                         'target_start': a, 'target_end': b, 'source_start': a+r['offset'],
                         'score': confidence, 'supporting_windows': len(r['points'])})
        if a+r['offset'] < -.01 or b+r['offset'] > source['duration'] + 1/source['fps']:
            review.append({'start': a, 'end': b, 'reason': 'Proposed range exceeds source media extent'})
        print('SEGMENT', source['name'], round(a, 3), round(b, 3),
              'source', round(a+r['offset'], 3), 'score', round(confidence, 3), flush=True)
    write(args.out, {'target_duration': duration, 'review': review, 'segments': segments,
                     'analysis': {'step': args.step, 'window': args.window,
                                  'min_segment': args.min_segment, 'min_score': args.min_score}})
    write(Path(args.out).with_name(Path(args.out).stem+'_matches.json'), points)
    print('PLAN', args.out, 'review intervals:', len(review), flush=True)


def lua(value):
    if value is None:
        return 'nil'
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, str):
        return '"'+value.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\r', '\\r').replace('\t', '\\t')+'"'
    if isinstance(value, dict):
        return '{'+','.join('['+lua(str(k))+']='+lua(v) for k, v in value.items())+'}'
    if isinstance(value, list):
        return '{'+','.join(lua(x) for x in value)+'}'
    if not math.isfinite(value):
        raise ValueError('Non-finite number in plan')
    return repr(value)


def pair(value):
    vals = [int(x) for x in value.split(',')]
    if len(vals) != 2 or min(vals) < 1 or len(set(vals)) != 2:
        raise ValueError('Specify two distinct positive track numbers')
    return vals


def compile_plan(args):
    manifest, plan = read(args.manifest), read(args.plan)
    snapshot = read(manifest['snapshot'])
    if plan.get('review') and not args.accept_review:
        raise ValueError('Inspect the review intervals, resolve them, or use --accept-review after checking evidence')
    fps = manifest['timeline_fps']
    if args.master_track != manifest['target']['master_track']:
        raise ValueError('Master track differs from prepare')
    vtracks, atracks = pair(args.video_tracks), pair(args.audio_tracks)
    if args.master_track in atracks:
        raise ValueError('Reference audio tracks must not include the master track')
    sources = {s['id']: s for s in manifest['sources']}
    segments = sorted(plan['segments'], key=lambda s: s['target_start'])
    if not segments or abs(segments[0]['target_start']) > 1/fps:
        raise ValueError('Plan must cover the target from its beginning')
    if abs(segments[-1]['target_end']-manifest['target']['duration']) > 1/fps:
        raise ValueError('Plan must cover the complete target duration')
    if any(s['target_end'] <= s['target_start'] for s in segments):
        raise ValueError('Plan contains a nonpositive segment duration')
    for a, b in zip(segments[:-1], segments[1:]):
        if abs(a['target_end']-b['target_start']) > 1e-6:
            raise ValueError('Plan has a gap or overlap')
    frames = [round(s['target_start']*fps) for s in segments] + [manifest['target']['frames']]
    nearest = min(range(len(frames)), key=lambda i: abs(frames[i]-args.resume_frame))
    if abs(frames[nearest]-args.resume_frame) <= args.boundary_tolerance_frames:
        frames[nearest] = args.resume_frame
    if args.resume_frame < 0 or args.resume_frame >= frames[-1]:
        raise ValueError('Resume frame is outside the target range')
    compiled = []
    previous = None
    selected_pair = args.first_pair
    for i, s in enumerate(segments):
        a, b = max(frames[i], args.resume_frame), frames[i+1]
        if b <= args.resume_frame:
            continue
        if b <= a:
            raise ValueError('A segment collapsed during frame quantization')
        source = sources[s['media_id']]
        if previous is not None and previous != s['media_id']:
            selected_pair = 1-selected_pair
        previous = s['media_id']
        source_sec = s['source_start'] + a/fps-s['target_start']
        if source_sec < 0 or source_sec+(b-a)/fps > source['duration']+1/source['fps']:
            raise ValueError('Source extent invalid: '+s['name'])
        compiled.append({'media_id': source['id'], 'name': s['name'], 'path': source['path'],
                         'start_frame': a, 'end_frame': b, 'duration': b-a,
                         'source_sec': source_sec, 'source_fps': source['fps'],
                         'source_frame': source_sec*source['fps'],
                         'source_span': (b-a)*source['fps']/fps + 1e-7,
                         'video_track': vtracks[selected_pair], 'audio_track': atracks[selected_pair]})
    if not compiled or compiled[0]['start_frame'] != args.resume_frame:
        raise ValueError('The new coverage does not begin at the resume frame')
    root = Path(args.out).resolve().parent
    root.mkdir(parents=True, exist_ok=True)
    export = Path(args.export).resolve()
    export.parent.mkdir(parents=True, exist_ok=True)
    mute = set(atracks)
    for t in snapshot['timeline']['tracks']:
        its = array(t['items'])
        if t['type'] == 'audio' and t['index'] != args.master_track and its and all(x.get('media') in sources for x in its):
            mute.add(t['index'])
    config = {'schema': 1, 'project_name': snapshot['name'], 'original_name': snapshot['timeline']['name'],
              'result_name': args.name, 'baseline': snapshot, 'timeline_fps': fps,
              'resume_frame': args.resume_frame, 'expected_frames': frames[-1],
              'master': manifest['target'], 'video_tracks': vtracks, 'audio_tracks': atracks,
              'mute_tracks': sorted(mute), 'segments': compiled, 'export_path': str(export),
              'backup_path': str(root/'before_apply.drp'), 'log_path': str(root/'apply_log.txt'),
              'after_snapshot': str(root/'after.json')}
    Path(args.out).write_text('return '+lua(config)+'\n', encoding='utf-8')
    write(Path(args.out).with_suffix('.json'), config)
    print('COMPILED', len(compiled), 'segments:', args.out, flush=True)


def verify(args):
    config = read(args.config)
    before, after = config['baseline'], read(config['after_snapshot'])
    origin = before['timeline']['start']
    if after['timeline']['name'] != config['result_name'] or after['timeline']['start'] != origin:
        raise ValueError('Unexpected result timeline')
    if config['original_name'] not in [x['name'] for x in after.get('timelines', [])]:
        raise ValueError('Original timeline missing')
    def identity(it):
        return (it.get('media'), it['start'], it['finish'], it['left'], it.get('enabled', True))
    for t in before['timeline']['tracks']:
        its = array(track(after, t['type'], t['index'])['items'])
        for it in array(t['items']):
            if identity(it) not in [identity(x) for x in its]:
                raise ValueError('An original item changed or disappeared')
    master_track = config['master']['master_track']
    if config['master']['already_placed']:
        a = array(track(before, 'audio', master_track)['items'])
        b = array(track(after, 'audio', master_track)['items'])
        if [identity(x) for x in a] != [identity(x) for x in b]:
            raise ValueError('Master clips changed')
    else:
        clips = array(track(after, 'audio', master_track)['items'])
        if len(clips) != 1 or clips[0]['media'] != config['master']['id'] or clips[0]['start'] != origin or clips[0]['finish'] != origin+config['expected_frames']:
            raise ValueError('New master clip is missing or has incorrect extents')
    if not track(after, 'audio', master_track).get('enabled'):
        raise ValueError('The master track is muted')
    for i in config['mute_tracks']:
        if track(after, 'audio', i).get('enabled', False):
            raise ValueError('A reference track is audible')
    for t in before['timeline']['tracks']:
        if t['type'] == 'audio' and t['index'] not in config['mute_tracks']+[master_track]:
            if t['enabled'] != track(after, 'audio', t['index']).get('enabled'):
                raise ValueError('An unrelated audio track changed state')
    for s in config['segments']:
        for kind, index in [('video', s['video_track']), ('audio', s['audio_track'])]:
            found = [x for x in array(track(after, kind, index)['items']) if x['start'] == origin+s['start_frame']]
            if len(found) != 1:
                raise ValueError('Missing or duplicate new clip: '+s['name'])
            it = found[0]
            if it['media'] != s['media_id'] or it['finish'] != origin+s['end_frame']:
                raise ValueError('Wrong media or end frame: '+s['name'])
            if abs(it.get('left_precise', it['left'])/config['timeline_fps']-s['source_sec']) > 2/config['timeline_fps']:
                raise ValueError('Source in-point error: '+s['name'])
    for kind, indices in [('video', config['video_tracks']), ('audio', config['audio_tracks'])]:
        clips = sorted([x for i in indices for x in array(track(after, kind, i)['items'])
                        if origin+config['resume_frame'] <= x['start'] < origin+config['expected_frames']], key=lambda x: x['start'])
        expected_start = origin+config['resume_frame']
        for it in clips:
            if it['start'] != expected_start:
                raise ValueError('Unintended gap or overlap on '+kind+' tracks')
            expected_start = it['finish']
        if expected_start != origin+config['expected_frames']:
            raise ValueError('Incomplete coverage')
    used = {s['media_id'] for s in config['segments']} | {config['master']['id']}
    for m in after['media']:
        if m['id'] in used:
            if m['props'].get('Online Status') != 'Online' or not Path(m['props']['File Path']).is_file():
                raise ValueError('Offline/missing media: '+m['name'])
    with zipfile.ZipFile(config['export_path']) as z:
        if z.testzip() is not None:
            raise ValueError('Corrupt DRP archive')
    print('PASS:', len(config['segments']), 'new linked clip pairs; baseline and master preserved; coverage and media valid.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('prepare', help='Decode project-linked media without modifying sources')
    p.add_argument('--snapshot', required=True)
    p.add_argument('--target', required=True, help='Exact media ID or file path')
    p.add_argument('--workdir', required=True)
    p.add_argument('--source-id', action='append')
    p.add_argument('--master-track', type=int, default=1)
    p.set_defaults(func=prepare)
    p = sub.add_parser('match', help='Propose all segments without examples')
    p.add_argument('--manifest', required=True)
    p.add_argument('--out', required=True)
    p.add_argument('--step', type=float, default=.5)
    p.add_argument('--window', type=float, default=2)
    p.add_argument('--min-segment', type=float, default=1.5)
    p.add_argument('--min-score', type=float, default=.5)
    p.set_defaults(func=match)
    p = sub.add_parser('compile', help='Create a reviewable Lua configuration, without applying it')
    p.add_argument('--manifest', required=True)
    p.add_argument('--plan', required=True)
    p.add_argument('--out', required=True)
    p.add_argument('--export', required=True)
    p.add_argument('--name', default='波形对齐完成')
    p.add_argument('--resume-frame', type=int, default=0)
    p.add_argument('--first-pair', type=int, choices=(0, 1), default=0)
    p.add_argument('--video-tracks', default='2,3')
    p.add_argument('--audio-tracks', default='2,4')
    p.add_argument('--master-track', type=int, default=1)
    p.add_argument('--boundary-tolerance-frames', type=int, default=3)
    p.add_argument('--accept-review', action='store_true')
    p.set_defaults(func=compile_plan)
    p = sub.add_parser('verify', help='Check the applied snapshot and DRP')
    p.add_argument('--config', required=True)
    p.set_defaults(func=verify)
    args = parser.parse_args()
    try:
        if getattr(args, 'step', 1) <= 0 or getattr(args, 'window', 1) <= 0 or getattr(args, 'min_segment', 1) <= 0:
            raise ValueError('Window, step and minimum segment must be positive')
        args.func(args)
    except (ValueError, KeyError, StopIteration, OSError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f'ERROR: {exc}\n')


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    main()
