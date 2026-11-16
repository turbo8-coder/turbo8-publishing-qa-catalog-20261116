"""Resolve approved author releases by immutable GitHub repository and owner ID."""
import hashlib
import io
import json
import os
import re
import stat
import subprocess
import html
import urllib.request
import zipfile



def require(ok, message):
    if not ok:
        raise ValueError(message)


def part(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,95}', value) and '..' not in value


def identity(recipe):
    for key in ('owner_id', 'repository_id'):
        require(type(recipe[key]) is int and 0 < recipe[key] < 2**63, 'Invalid GitHub identity')
    require(part(recipe['cart_id']), 'Invalid author cartridge ID')
    key = f"{recipe['repository_id']}/{recipe['cart_id']}"
    return 'u%d-%s' % (recipe['owner_id'], hashlib.sha256(key.encode()).hexdigest()[:32])


def read(url, limit):
    # Anonymous requests only: GitHub redirects downloads to its asset CDN.
    request = urllib.request.Request(url, headers={'User-Agent': 'Turbo8-Catalog'})
    with urllib.request.urlopen(request, timeout=30) as response:
        data = response.read(limit + 1)
    require(len(data) <= limit, 'Download exceeds byte limit')
    return data


def upstream(recipe):
    info = json.loads(subprocess.check_output(['gh', 'api', 'repositories/%d' % recipe['repository_id']]))
    require(info['id'] == recipe['repository_id'] and info['owner']['id'] == recipe['owner_id']
            and not info['private'], 'Source ownership changed')
    name = info['full_name']
    require(len(name.split('/')) == 2 and all(part(p) for p in name.split('/')), 'Invalid repository name')
    return 'https://github.com/' + name + '/releases/'


def latest(recipe, base):
    catalog = json.loads(read(base + 'latest/download/catalog-v2.json', 1024 * 1024))
    require(catalog['schema'] == 1, 'Invalid author catalog')
    rows = [item for item in catalog['cartridges'] if item['id'] == recipe['cart_id']]
    require(len(rows) == 1, 'Expected one matching cartridge in author catalog')
    return rows[0]


def candidate(recipe, entry, base, pipeline):
    cart = identity(recipe)
    pipeline.version(entry['version'])
    require(part(entry['tag']) and part(entry['asset']) and entry['asset'].endswith('.zip'), 'Invalid release asset')
    require(re.fullmatch(r'[a-f0-9]{64}', entry['sha256']), 'Invalid source digest')
    raw = read(base + 'download/' + entry['tag'] + '/' + entry['asset'], pipeline.MAX_ZIP)
    require(hashlib.sha256(raw).hexdigest() == entry['sha256'], 'Source ZIP checksum mismatch')
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        infos = archive.infolist()
        require(len(infos) < pipeline.MAX_FILES and sum(x.file_size for x in infos) <= pipeline.MAX_EXPANDED,
                'Expanded ZIP exceeds limits')
        files = {}
        for x in infos:
            require(pipeline.safe_path(x.filename) and x.file_size <= pipeline.MAX_FILE
                    and not stat.S_ISLNK(x.external_attr >> 16), 'Invalid ZIP member')
            path = 'cartridge/' + x.filename
            require(path not in files, 'Duplicate ZIP path')
            files[path] = archive.read(x)
    index = pipeline.read_json(files['cartridge/index.json'])
    # The author template versions releases independently of the local Player file.
    index['cartridgeVersion'] = entry['version']
    files['cartridge/index.json'] = pipeline.json_bytes(index)
    meta = dict(schema=1, id=cart, author_id=recipe['owner_id'], version=entry['version'], title=index['name'])
    files['submission.json'] = pipeline.json_bytes(meta)
    return pipeline.validate(files, cart)


def review(recipe, filename, actor, pipeline):
    require(filename == identity(recipe) + '.json' and recipe['owner_id'] == actor, 'Only the source owner may apply')
    base = upstream(recipe)
    current = latest(recipe, base)
    require(all(current[k] == recipe[k] for k in ('version', 'tag', 'asset', 'sha256')),
            'Refresh the author source and apply with the current release')
    return candidate(recipe, recipe, base, pipeline)


def resolve(recipe, prior, pipeline):
    if prior and recipe.get('paused') is True:
        return None
    base = upstream(recipe)
    entry = latest(recipe, base) if prior else recipe
    if prior:
        new = pipeline.version(entry['version'])
        old = pipeline.version(prior['version'])
        require(new >= old, 'Upstream version decreased')
        if new == old:
            require(entry['sha256'] == prior['source_sha256'], 'Published version changed its bytes')
            return None
    meta, raw, cover, resolution = candidate(recipe, entry, base, pipeline)
    pipeline.cover_assets.optimize(cover)
    cart = identity(recipe)
    asset = f"{cart}-v{meta['version']}.zip"
    item = dict(id=cart, title=meta['title'], version=meta['version'], author_id=meta['author_id'],
                tag='cart-' + asset[:-4], asset=asset, bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest(),
                resolution=resolution, source_sha256=entry['sha256'])
    return item, raw, cover


def collect(root, output, entries, previous, unlisted, commit, pipeline):
    prior_entries = {item['id']: item for item in previous['cartridges']}
    for path in sorted((root / 'tracked').glob('*.json')):
        recipe = json.loads(path.read_text())
        cart = identity(recipe)
        require(path.stem == cart and cart not in entries, 'Duplicate or invalid tracked identity')
        if cart in unlisted:
            continue
        prior = prior_entries.get(cart)
        try:
            update = resolve(recipe, prior, pipeline)
            if update:
                item, raw, cover = update
                pipeline.release_asset(item['tag'], item['asset'], raw, commit)
        except Exception as error:
            # Keep unrelated releases moving. Report which source needs attention.
            message = f'{cart}: {type(error).__name__}: {error}'
            print(ascii(message), flush=True)
            if os.environ.get('GITHUB_STEP_SUMMARY'):
                with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as summary:
                    summary.write('<pre>' + html.escape(message[:500]) + '</pre>\n')
            update = None
        if not update:
            if not prior:
                continue
            item = prior.copy()
            digest = item['cover']['sha256']
            require(re.fullmatch('[a-f0-9]{64}', digest), 'Invalid prior cover')
            cover = read(f'https://github.com/{pipeline.REPO}/releases/download/online-covers-v1/{digest}.png', 128 * 1024)
            require(hashlib.sha256(cover).hexdigest() == digest, 'Prior cover checksum mismatch')
        (output / 'covers').mkdir(exist_ok=True)
        (output / 'covers' / (cart + '.png')).write_bytes(cover)
        entries[cart] = item
