package vard.agent;

import net.bytebuddy.agent.builder.AgentBuilder;
import net.bytebuddy.asm.Advice;
import net.bytebuddy.implementation.bytecode.assign.Assigner;
import static net.bytebuddy.matcher.ElementMatchers.*;

import java.io.*;
import java.lang.instrument.Instrumentation;
import java.util.*;
import java.util.concurrent.ConcurrentHashMap;

/**
 * VARD instrumentation agent — the ground-truth runtime leg. Attached via -javaagent during `vard test`.
 * Unlike the stack-sampling fallback (sampler-VardSampler.java.txt), this DETERMINISTICALLY observes every
 * call of every app-package method (fixes the sampling miss on sub-ms methods) and captures BOUNDED arg/return
 * VALUES — the agent-uncatchable fact a static reader can never reconstruct ("chose A(5400) over B(5100)").
 *
 * It records, per app method: true call count, true caller→callee edges (via a thread-local call stack),
 * and a capped set of observed (args ⇒ return) value samples. Values are type-bounded — primitives, boxed,
 * String (truncated), enum, Number — and arbitrary objects are NEVER toString'd (no PII, no huge dumps, no
 * side effects). It also stamps a config fingerprint (active Spring profile) so ACTUAL can't masquerade a
 * test-profile run as prod truth. Output: per-PID JSONL, extending the sampler's schema.
 *
 * System props: vard.pkgs=com.foo,com.bar (instrument only these prefixes; required for sane scope)
 *               vard.out=path   vard.values=*|substr,substr (which methods capture values; default *)
 *               vard.maxsamples=N (distinct value samples per method; default 8)
 */
public class VardAgent {

    public static void premain(String args, Instrumentation inst) { install(args, inst); }
    public static void agentmain(String args, Instrumentation inst) { install(args, inst); }   // live attach

    private static void install(String args, Instrumentation inst) {
        // Options arrive two ways: -D system props (premain via JAVA_TOOL_OPTIONS) OR a "k=v;k=v" args string
        // (agentmain via VirtualMachine.loadAgent — a live attach can't set -D on an already-running JVM).
        Map<String, String> a = parseArgs(args);
        String pkgsRaw = opt(a, "pkgs", "");
        final String[] pkgs = pkgsRaw.isEmpty() ? new String[0] : pkgsRaw.split(",");
        final String out = opt(a, "out", ".vard/runtime-trace.jsonl");
        Collector.configure(opt(a, "values", "*"), Integer.parseInt(opt(a, "maxsamples", "8")),
                            opt(a, "env", ""));
        final int flushSecs = Integer.parseInt(opt(a, "flush", "0"));   // >0 → periodic snapshot (live attach)

        net.bytebuddy.matcher.ElementMatcher.Junction<net.bytebuddy.description.type.TypeDescription> typeM =
                not(nameStartsWith("vard.")).and(not(nameStartsWith("vard.shaded.")))
                .and(not(nameStartsWith("java."))).and(not(nameStartsWith("jdk.")))
                .and(not(nameStartsWith("sun."))).and(not(nameContains("$$")));
        if (pkgs.length > 0) {
            net.bytebuddy.matcher.ElementMatcher.Junction<net.bytebuddy.description.type.TypeDescription> any = none();
            for (String p : pkgs) any = any.or(nameStartsWith(p.trim()));
            typeM = typeM.and(any);
        }

        System.err.println("[vard-agent] installing (pkgs=" + pkgsRaw + ", env=" + opt(a, "env", "")
                           + ", flush=" + flushSecs + "s)");
        new AgentBuilder.Default()
                // disableClassFormatChanges → REDEFINE (no added methods/fields), which RETRANSFORMATION of
                // ALREADY-LOADED classes requires. Essential for live attach (agentmain), harmless for premain.
                .disableClassFormatChanges()
                .with(AgentBuilder.RedefinitionStrategy.RETRANSFORMATION)
                // default redefinition listener ignores per-class transform failures — on a real app some
                // classes (proxies, generated) legitimately can't be instrumented; one failure must not abort.
                .ignore(nameStartsWith("vard.").or(nameStartsWith("net.bytebuddy.")))
                .type(typeM)
                .transform((builder, td, cl, mod, pd) -> builder.visit(
                        Advice.to(MethodAdvice.class).on(
                                isMethod().and(not(isAbstract())).and(not(isNative()))
                                          .and(not(isConstructor())).and(not(isTypeInitializer())))))
                .installOn(inst);

        Runtime.getRuntime().addShutdownHook(new Thread(() -> Collector.dump(out)));
        if (flushSecs > 0) {                              // live attach: snapshot the trace while the app runs
            Thread t = new Thread(() -> {
                while (true) {
                    try { Thread.sleep(flushSecs * 1000L); } catch (InterruptedException e) { return; }
                    Collector.dump(out, false);              // quiet: a line every flush would spam the server log
                }
            });
            t.setDaemon(true);
            t.setName("vard-flush");
            t.start();
        }
    }

    /** Parse a "k=v;k2=v2" agent-args string (agentmain) into a map. Null/empty → empty map. */
    private static Map<String, String> parseArgs(String args) {
        Map<String, String> m = new HashMap<>();
        if (args == null || args.isEmpty()) return m;
        for (String kv : args.split(";")) {
            int i = kv.indexOf('=');
            if (i > 0) m.put(kv.substring(0, i).trim(), kv.substring(i + 1).trim());
        }
        return m;
    }

    /** Option lookup: agent-args map first, then -Dvard.<key>, then default. */
    private static String opt(Map<String, String> a, String key, String def) {
        if (a.containsKey(key)) return a.get(key);
        return System.getProperty("vard." + key, def);
    }

    /** Inlined into every instrumented method. Keep tiny; all real work is in Collector (never throws out). */
    public static class MethodAdvice {
        @Advice.OnMethodEnter
        public static void enter(@Advice.Origin("#t.#m") String qual) {
            Collector.enter(qual);
        }
        @Advice.OnMethodExit(onThrowable = Throwable.class)
        public static void exit(@Advice.Origin("#t.#m") String qual,
                                @Advice.AllArguments(typing = Assigner.Typing.DYNAMIC) Object[] argv,
                                @Advice.Return(typing = Assigner.Typing.DYNAMIC, readOnly = true) Object ret,
                                @Advice.Thrown Throwable thrown) {
            Collector.exit(qual, argv, ret, thrown);
        }
    }

    /** Static collector on the system classloader (visible to instrumented app classes). All methods swallow
     *  their own errors — instrumentation must never break the app under test. */
    public static class Collector {
        static final Map<String, long[]> methods = new ConcurrentHashMap<>();
        static final Map<String, long[]> edges = new ConcurrentHashMap<>();
        static final Map<String, Map<String, long[]>> values = new ConcurrentHashMap<>();
        static final ThreadLocal<Deque<String>> STACK = ThreadLocal.withInitial(ArrayDeque::new);
        static String[] valFilter = {"*"};
        static int maxSamples = 8;
        static String env = "";

        static void configure(String values, int max, String envLabel) {
            valFilter = (values == null || values.isEmpty()) ? new String[]{"*"} : values.split(",");
            maxSamples = Math.max(1, max);
            env = envLabel == null ? "" : envLabel;
        }

        public static void enter(String qual) {        // public: the Advice body is INLINED into app classes
            try {
                Deque<String> st = STACK.get();
                String caller = st.peek();
                methods.computeIfAbsent(qual, k -> new long[1])[0]++;
                if (caller != null && !caller.equals(qual))
                    edges.computeIfAbsent(caller + ">" + qual, k -> new long[1])[0]++;
                st.push(qual);
            } catch (Throwable ignore) { }
        }

        // method names that handle secrets — their captured args/returns are REDACTED (a captured decrypted
        // password is exactly the leak we must not produce). Bounded types already block object dumps; this
        // guards the string/number values that ARE captured.
        static final java.util.regex.Pattern SECRET = java.util.regex.Pattern.compile(
                "(?i)(password|passwd|secret|token|credential|apikey|api_key|privatekey|decrypt|encrypt|cipher|signature)");

        public static void exit(String qual, Object[] argv, Object ret, Throwable thrown) {
            try {
                Deque<String> st = STACK.get();
                if (!st.isEmpty()) st.pop();
                if (!captureValues(qual)) return;
                String key = SECRET.matcher(qual).find()
                        ? "<redacted: secret-handling method>"
                        : sig(argv) + " => " + (thrown != null
                                ? "throw " + thrown.getClass().getSimpleName() : safe(ret));
                Map<String, long[]> m = values.computeIfAbsent(qual, k -> new ConcurrentHashMap<>());
                long[] c = m.get(key);
                if (c != null) c[0]++;
                else if (m.size() < maxSamples) m.computeIfAbsent(key, k -> new long[1])[0]++;
            } catch (Throwable ignore) { }
        }

        static boolean captureValues(String qual) {
            for (String f : valFilter) {
                if (f.equals("*")) return true;
                if (!f.isEmpty() && qual.contains(f.trim())) return true;
            }
            return false;
        }

        /** Type-bounded, PII-safe rendering. Numbers/booleans/strings/enums only; never toString arbitrary
         *  objects (could leak PII, be huge, or have side effects) — emit just the type name instead. */
        static String safe(Object o) {
            if (o == null) return "null";
            if (o instanceof String) {
                String s = (String) o;
                return "\"" + esc(s.length() > 64 ? s.substring(0, 64) + "…" : s) + "\"";
            }
            if (o instanceof Number || o instanceof Boolean || o instanceof Character) return o.toString();
            if (o instanceof Enum) return ((Enum<?>) o).name();
            return "<" + o.getClass().getSimpleName() + ">";
        }

        static String sig(Object[] argv) {
            if (argv == null || argv.length == 0) return "()";
            StringBuilder b = new StringBuilder("(");
            for (int i = 0; i < argv.length; i++) {
                if (i > 0) b.append(", ");
                b.append(safe(argv[i]));
            }
            return b.append(")").toString();
        }

        static void dump(String out) { dump(out, true); }

        static void dump(String out, boolean announce) {
            if (methods.isEmpty() && edges.isEmpty()) return;     // a JVM that ran no app code
            try {
                long pid;
                try { pid = ProcessHandle.current().pid(); } catch (Throwable t) { pid = System.nanoTime(); }
                File f = new File(out + "." + pid);
                File tmp = new File(out + "." + pid + ".tmp");    // write tmp + atomic rename so a concurrent
                if (f.getParentFile() != null) f.getParentFile().mkdirs();  // reader (live-attach flush) never
                try (PrintWriter w = new PrintWriter(new FileWriter(tmp))) {  // sees a half-written file
                    String prof = firstNonEmpty(System.getProperty("spring.profiles.active"),
                                                System.getenv("SPRING_PROFILES_ACTIVE"));
                    w.println("{\"t\":\"config\",\"profile\":\"" + esc(prof == null ? "" : prof)
                              + "\",\"mode\":\"instrument\",\"env\":\"" + esc(env) + "\"}");
                    for (Map.Entry<String, long[]> m : methods.entrySet())
                        w.println("{\"t\":\"method\",\"qual\":\"" + esc(m.getKey()) + "\",\"hits\":" + m.getValue()[0] + "}");
                    for (Map.Entry<String, long[]> e : edges.entrySet()) {
                        String[] p = e.getKey().split(">", 2);
                        w.println("{\"t\":\"edge\",\"caller\":\"" + esc(p[0]) + "\",\"callee\":\"" + esc(p[1])
                                  + "\",\"n\":" + e.getValue()[0] + "}");
                    }
                    for (Map.Entry<String, Map<String, long[]>> v : values.entrySet()) {
                        StringBuilder b = new StringBuilder("{\"t\":\"value\",\"qual\":\"" + esc(v.getKey()) + "\",\"samples\":[");
                        boolean first = true;
                        for (Map.Entry<String, long[]> s : v.getValue().entrySet()) {
                            if (!first) b.append(",");
                            first = false;
                            b.append("{\"v\":\"").append(esc(s.getKey())).append("\",\"n\":").append(s.getValue()[0]).append("}");
                        }
                        w.println(b.append("]}").toString());
                    }
                }
                try {
                    java.nio.file.Files.move(tmp.toPath(), f.toPath(),
                            java.nio.file.StandardCopyOption.ATOMIC_MOVE,
                            java.nio.file.StandardCopyOption.REPLACE_EXISTING);
                } catch (Throwable mv) {                          // ATOMIC_MOVE unsupported → best-effort rename
                    if (!tmp.renameTo(f)) tmp.delete();
                }
                if (announce)
                    System.err.println("[vard-agent] wrote runtime trace: " + out + "." + pid
                                       + " (" + methods.size() + " methods, " + edges.size() + " edges, "
                                       + values.size() + " valued)");
            } catch (Throwable ex) { System.err.println("[vard-agent] dump failed: " + ex); }
        }

        static String firstNonEmpty(String a, String b) {
            return (a != null && !a.isEmpty()) ? a : (b != null && !b.isEmpty() ? b : null);
        }
        static String esc(String s) {
            return s.replace("\\", "\\\\").replace("\"", "\\\"").replace("\n", " ").replace("\r", " ");
        }
    }
}
