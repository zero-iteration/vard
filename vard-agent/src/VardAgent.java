import java.lang.instrument.Instrumentation;
import java.io.*;
import java.util.*;
import java.util.concurrent.ConcurrentHashMap;

/** Zero-dependency runtime listener. Attached via -javaagent during `mvn test`. Samples thread stacks
 *  and records (a) which methods actually executed and (b) real caller->callee edges — ground truth a
 *  static reader cannot reconstruct (resolves dynamic dispatch / interface->impl). Writes JSONL on exit.
 *  System props: vard.pkgs=comma,prefixes (keep only these class prefixes); vard.out=path; vard.ms=interval */
public class VardAgent {
    static final Map<String, long[]> methods = new ConcurrentHashMap<>();   // qual -> [hits]
    static final Map<String, long[]> edges = new ConcurrentHashMap<>();     // caller>callee -> [hits]
    static volatile boolean running = true;

    public static void premain(String args, Instrumentation inst) {
        String pkgsRaw = System.getProperty("vard.pkgs", "");
        final String[] pkgs = pkgsRaw.isEmpty() ? new String[0] : pkgsRaw.split(",");
        final String out = System.getProperty("vard.out", ".vard/runtime-trace.jsonl");
        final long ms = Long.parseLong(System.getProperty("vard.ms", "2"));
        Thread t = new Thread(() -> {
            while (running) {
                for (StackTraceElement[] st : Thread.getAllStackTraces().values()) {
                    for (int i = 0; i < st.length; i++) {
                        String callee = qual(st[i]);
                        if (!keep(st[i], pkgs)) continue;
                        methods.computeIfAbsent(callee, k -> new long[1])[0]++;
                        // st[i+1] called st[i]: caller is the deeper frame
                        for (int j = i + 1; j < st.length; j++) {
                            if (keep(st[j], pkgs)) {
                                String caller = qual(st[j]);
                                if (!caller.equals(callee))
                                    edges.computeIfAbsent(caller + ">" + callee, k -> new long[1])[0]++;
                                break;
                            }
                        }
                    }
                }
                try { Thread.sleep(ms); } catch (InterruptedException e) { break; }
            }
        });
        t.setDaemon(true);
        t.setName("vard-sampler");
        t.start();
        Runtime.getRuntime().addShutdownHook(new Thread(() -> { running = false; dump(out); }));
    }

    static String qual(StackTraceElement e) { return e.getClassName() + "." + e.getMethodName(); }

    static boolean keep(StackTraceElement e, String[] pkgs) {
        String c = e.getClassName();
        if (c.startsWith("java.") || c.startsWith("jdk.") || c.startsWith("sun.")
            || c.startsWith("VardAgent") || c.contains("$$")) return false;
        if (pkgs.length == 0) return true;
        for (String p : pkgs) if (c.startsWith(p)) return true;
        return false;
    }

    static void dump(String out) {
        if (methods.isEmpty() && edges.isEmpty()) return;    // a JVM that ran no app code (e.g. the maven process)
        try {
            // Per-PID file: a build spawns several JVMs (the maven process + each forked Surefire JVM) and
            // each runs this shutdown hook. Writing to a shared path would clobber; suffixing the PID lets
            // `vard test` merge every JVM's trace. Empty traces (the maven JVM, filtered to app pkgs) are no-ops.
            long pid;
            try { pid = ProcessHandle.current().pid(); } catch (Throwable t) { pid = System.nanoTime(); }
            out = out + "." + pid;
            File f = new File(out);
            if (f.getParentFile() != null) f.getParentFile().mkdirs();
            try (PrintWriter w = new PrintWriter(new FileWriter(f))) {
                for (Map.Entry<String, long[]> m : methods.entrySet())
                    w.println("{\"t\":\"method\",\"qual\":\"" + esc(m.getKey()) + "\",\"hits\":" + m.getValue()[0] + "}");
                for (Map.Entry<String, long[]> e : edges.entrySet()) {
                    String[] p = e.getKey().split(">", 2);
                    w.println("{\"t\":\"edge\",\"caller\":\"" + esc(p[0]) + "\",\"callee\":\"" + esc(p[1])
                              + "\",\"n\":" + e.getValue()[0] + "}");
                }
            }
            System.err.println("[vard-agent] wrote runtime trace: " + out
                               + " (" + methods.size() + " methods, " + edges.size() + " edges)");
        } catch (IOException ex) { System.err.println("[vard-agent] dump failed: " + ex); }
    }

    static String esc(String s) { return s.replace("\\", "\\\\").replace("\"", "\\\""); }
}
