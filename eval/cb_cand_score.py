#!/usr/bin/env python3
"""Score agent-from-pool vs codefirst on the ContextBench Java set (gold from /tmp/cb_cand_gold.json)."""
import json, os
P = "core/src/main/java/com/alibaba/fastjson2/"
J = "src/main/java/com/fasterxml/jackson/core/"
M = "src/main/java/org/mockito/"
SEL = {
 "java-gfix__8b7c": [P+"JSON.java::JSON.parseObject", P+"JSON.java::JSON.parse", P+"JSON.java::JSON",
   P+"JSON.java::JSON.parseArray", P+"JSONReader.java::JSONReader.Context.getObjectReader",
   P+"JSONReader.java::JSONReader.getObjectReader", P+"JSONReader.java::JSONReader.of",
   P+"JSONReader.java::JSONReader.Context.Context", P+"JSONReader.java::JSONReader.read",
   P+"JSONReader.java::JSONReader.getObjectReaderAutoType"],
 "java-gfix__ef56": [P+"writer/ObjectWriterImplInstant.java::ObjectWriterImplInstant.write",
   P+"writer/FieldWriterDate.java::FieldWriterDate.writeDate",
   P+"writer/ObjectWriterImplDate.java::ObjectWriterImplDate.write",
   P+"JSONWriter.java::JSONWriter.Context.isFormatyyyyMMddhhmmss19",
   P+"JSONWriter.java::JSONWriter.writeMillis", P+"JSONWriter.java::JSONWriter.illegalYear",
   P+"util/DateUtils.java::DateUtils.formatYMDHMS19", P+"util/DateUtils.java::DateUtils.format",
   P+"util/DateUtils.java::DateUtils.formatYMD10", P+"JSONWriterUTF16.java::JSONWriterUTF16.write0"],
 "java-gfix__5eea": [J+"json/ReaderBasedJsonParser.java::ReaderBasedJsonParser._parseName",
   J+"json/ReaderBasedJsonParser.java::ReaderBasedJsonParser._parseName2",
   J+"json/ReaderBasedJsonParser.java::ReaderBasedJsonParser._handleOddName",
   J+"json/ReaderBasedJsonParser.java::ReaderBasedJsonParser._handleOddName2",
   J+"json/ReaderBasedJsonParser.java::ReaderBasedJsonParser._parseAposName",
   J+"json/UTF8StreamJsonParser.java::UTF8StreamJsonParser._handleOddName",
   J+"json/UTF8StreamJsonParser.java::UTF8StreamJsonParser._parseAposName",
   J+"json/UTF8StreamJsonParser.java::UTF8StreamJsonParser.addName",
   J+"json/UTF8StreamJsonParser.java::UTF8StreamJsonParser.parseEscapedName",
   J+"sym/ByteQuadsCanonicalizer.java::ByteQuadsCanonicalizer.addName"],
 "java-gfix__6ba3": [J+"JsonFactory.java::JsonFactory.setInputDecorator",
   J+"JsonFactory.java::JsonFactory.setCharacterEscapes", J+"JsonFactory.java::JsonFactory.setRootValueSeparator",
   J+"JsonFactory.java::JsonFactory.setCodec", J+"JsonFactory.java::JsonFactory.setOutputDecorator",
   J+"JsonFactory.java::JsonFactory.JsonFactory", J+"JsonFactory.java::JsonFactory.copy",
   J+"JsonFactory.java::JsonFactory.readResolve", J+"JsonFactory.java::JsonFactory",
   J+"JsonFactory.java::JsonFactory._createParser"],
 "java-gfix__1665": [M+"plugins/DoNotMockEnforcer.java::DoNotMockEnforcer.checkTypeForDoNotMockViolation",
   M+"plugins/DoNotMockEnforcer.java::DoNotMockEnforcer",
   M+"internal/configuration/DefaultDoNotMockEnforcer.java::DefaultDoNotMockEnforcer.checkTypeForDoNotMockViolation",
   M+"internal/configuration/DefaultDoNotMockEnforcer.java::DefaultDoNotMockEnforcer",
   M+"internal/MockitoCore.java::MockitoCore.checkDoNotMockAnnotation",
   M+"internal/MockitoCore.java::MockitoCore.checkDoNotMockAnnotationForType",
   M+"internal/MockitoCore.java::MockitoCore.mockStatic",
   M+"internal/configuration/plugins/PluginRegistry.java::PluginRegistry.getDoNotMockEnforcer",
   M+"internal/creation/bytebuddy/InlineBytecodeGenerator.java::InlineBytecodeGenerator.mockClassStatic"],
}
meta = json.load(open("/tmp/cb_cand_gold.json"))
fof = lambda q: q.split("::")[0]
print(f"  {'instance':18s} {'gold':>4s} | {'cf@8 file':>9s} {'agent file':>10s} | {'cf@8 sym':>8s} {'agent sym':>9s} | {'ceil':>4s}")
acc = {k: 0.0 for k in ("scf", "sag", "fcf", "fag", "ce")}
n = 0
for bid, m in meta.items():
    gold = set(m["gold"]);
    if not gold: continue
    n += 1; sel = set(SEL.get(bid, [])); cf = set(m["cf8"])
    gf = {fof(g) for g in gold}
    r = lambda a, b: len(a & b) / len(b) if b else 0.0
    scf, sag = r(cf, gold), r(sel, gold)
    fcf, fag = r({fof(x) for x in cf}, gf), r({fof(x) for x in sel}, gf)
    for k, v in (("scf", scf), ("sag", sag), ("fcf", fcf), ("fag", fag), ("ce", m["ceiling"])):
        acc[k] += v
    print(f"  {bid:18s} {len(gold):>4d} | {fcf:9.2f} {fag:10.2f} | {scf:8.2f} {sag:9.2f} | {m['ceiling']:4.2f}")
g = lambda k: acc[k] / n
print(f"\n  {'AVERAGE':18s} {'':>4s} | {g('fcf'):9.2f} {g('fag'):10.2f} | {g('scf'):8.2f} {g('sag'):9.2f} | {g('ce'):4.2f}")
print("\n  (ContextBench issues often NAME the target API -> content baseline is already strong)")
