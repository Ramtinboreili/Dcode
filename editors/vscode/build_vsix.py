#!/usr/bin/env python3
"""Pack this folder into dcode-<version>.vsix (a zip VS Code installs: no node or vsce needed).

    python3 editors/vscode/build_vsix.py [output folder]
    code --install-extension dcode-<version>.vsix
"""

from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

HERE = Path(__file__).resolve().parent
FILES = ["package.json", "extension.js", "README.md"]


def main() -> int:
    manifest = json.loads((HERE / "package.json").read_text())
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{manifest['name']}-{manifest['version']}.vsix"
    identity = (f'<Identity Language="en-US" Id="{escape(manifest["name"])}" Version="{escape(manifest["version"])}" '
                f'Publisher="{escape(manifest["publisher"])}"/>')
    vsix_manifest = f"""<?xml version="1.0" encoding="utf-8"?>
<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011">
  <Metadata>
    {identity}
    <DisplayName>{escape(manifest["displayName"])}</DisplayName>
    <Description xml:space="preserve">{escape(manifest["description"])}</Description>
    <Categories>{escape(",".join(manifest.get("categories", [])))}</Categories>
    <Properties>
      <Property Id="Microsoft.VisualStudio.Code.Engine" Value="{escape(manifest["engines"]["vscode"])}"/>
      <Property Id="Microsoft.VisualStudio.Code.ExtensionKind" Value="workspace"/>
    </Properties>
  </Metadata>
  <Installation><InstallationTarget Id="Microsoft.VisualStudio.Code"/></Installation>
  <Dependencies/>
  <Assets>
    <Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json" Addressable="true"/>
    <Asset Type="Microsoft.VisualStudio.Services.Content.Details" Path="extension/README.md" Addressable="true"/>
  </Assets>
</PackageManifest>
"""
    content_types = """<?xml version="1.0" encoding="utf-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="json" ContentType="application/json"/>
  <Default Extension="js" ContentType="application/javascript"/>
  <Default Extension="md" ContentType="text/markdown"/>
  <Default Extension="txt" ContentType="text/plain"/>
  <Default Extension="vsixmanifest" ContentType="text/xml"/>
</Types>
"""
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("extension.vsixmanifest", vsix_manifest)
        for name in FILES:
            archive.write(HERE / name, f"extension/{name}")
    print(target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
