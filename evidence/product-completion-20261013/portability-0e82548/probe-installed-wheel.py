"""Offline imports and installed PDF helper execution, no source checkout imports."""
from __future__ import annotations
import importlib
import importlib.metadata
import importlib.util
import hashlib
import json
from pathlib import Path
import sys

kind=sys.argv[1]
modules=[
    'modelbuilder.dataset_streaming','modelbuilder.parquet_stream',
    'modelbuilder.iterations','modelbuilder.backend.native_invocations',
    'modelbuilder.backend.dependency_files','modelbuilder.backend.components.transformers_loader',
    'modelbuilder.backend.components.transformers_training','modelbuilder.backend.components.transformers_inference',
    'modelbuilder.backend.components.mlx','modelbuilder.backend.components.mlx_bridge',
    'modelbuilder.backend.components.mlx_components','modelbuilder.backend.components.mlx_probe',
    'modelbuilder.backend.components.mlx_worker',
    'modelbuilder.remote.modal_runtime','modelbuilder.remote.modal_provider','modelbuilder.remote.modal_build_inputs',
]
if kind=='service':
    modules+=['modelbuilder.service','modelbuilder.api','modelbuilder.mcp_server',
              'modelbuilder.research_documents','modelbuilder.research_document_decode']
else:
    modules+=['modelbuilder.backend.worker','modelbuilder.backend.conditional_worker','modelbuilder.backend.sdk']
locations={}
for name in modules:
    module=importlib.import_module(name)
    path=Path(module.__file__).resolve()
    assert path.is_relative_to(Path(sys.prefix).resolve()), (name,str(path),sys.prefix)
    locations[name]=str(path)
from modelbuilder.backend.components import component_catalog
catalog=sorted(entry['id'] for entry in component_catalog())
assert {'modelbuilder.transformers.train','modelbuilder.transformers.evaluate','modelbuilder.transformers.export'} <= set(catalog)
assert {'modelbuilder.mlx.train','modelbuilder.mlx.evaluate','modelbuilder.mlx.generate','modelbuilder.mlx.export'} <= set(catalog)
report={'status':'PASS','kind':kind,'prefix':sys.prefix,'module_locations':locations,'catalog':catalog}
from modelbuilder.backend.components import mlx_bridge
profile=Path(mlx_bridge.__file__).with_name('mlx-darwin-arm64.lock')
source=Path(sys.argv[3])
relative='src/modelbuilder/backend/components/mlx-darwin-arm64.lock'
raw=profile.read_bytes()
assert raw==(source/relative).read_bytes()==(source/'requirements/profiles/mlx-darwin-arm64.lock').read_bytes()
digest=hashlib.sha256(raw).hexdigest()
assert digest==mlx_bridge.PROFILE_SHA256
assert not any(name=='mlx' or name.startswith('mlx.') for name in sys.modules)
report['installed_package_data_sha256']={relative:digest}
report['mlx']={'inert_module_imports_verified':True,'adjacent_profile_matches_source_and_requirements':True,
               'native_framework_imported':False,'metal_hardware_verified':False,'profile_sha256':digest,
               'scope':'Linux packaging/import verification only; no MLX computation or Metal hardware claim'}
if kind=='backend':
    assert importlib.util.find_spec('modelbuilder.service') is None
    report['trainer_overlay_version']=importlib.metadata.version('accelerate')
    assert report['trainer_overlay_version']=='1.11.0'
else:
    from modelbuilder.research_documents import extract_pdf
    text='Installed wheel PDF extraction.'
    content=b'BT /F1 12 Tf 40 700 Td ('+text.encode()+b') Tj ET'
    objects=[b'<< /Type /Catalog /Pages 2 0 R >>',b'<< /Type /Pages /Count 1 /Kids [3 0 R] >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
        b'<< /Length '+str(len(content)).encode()+b' >>\nstream\n'+content+b'\nendstream']
    raw=b'%PDF-1.4\n';offsets=[]
    for number,value in enumerate(objects,1):
        offsets.append(len(raw));raw+=f'{number} 0 obj\n'.encode()+value+b'\nendobj\n'
    xref=len(raw);raw+=f'xref\n0 {len(objects)+1}\n0000000000 65535 f \n'.encode()
    raw+=b''.join(f'{offset:010d} 00000 n \n'.encode() for offset in offsets)
    raw+=f'trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode()
    extraction=extract_pdf(raw,{'ocr_mode':'disabled','max_pages':100,'max_ocr_pages':20})
    assert extraction['text']==text
    report['installed_pdf_helper']={'text':extraction['text'],'tools':extraction['tools'],'pages':len(extraction['pages'])}
if len(sys.argv)>2:
    Path(sys.argv[2]).write_text(json.dumps(report,indent=2)+'\n')
else:
    print(json.dumps(report,indent=2))
