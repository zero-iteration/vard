package vard.agent;

import com.sun.tools.attach.VirtualMachine;

/**
 * Live-attach launcher: loads the VARD agent into an ALREADY-RUNNING JVM via the Attach API, so you can
 * trace a local/staging server without restarting it. Run as:
 *     java -cp vard-agent.jar vard.agent.Attacher <pid> <agentJarPath> "<k=v;k=v agent args>"
 * The target must be a JVM owned by the same user. Uses the jdk.attach module (present in any JDK).
 */
public class Attacher {
    public static void main(String[] args) throws Exception {
        if (args.length < 2) {
            System.err.println("usage: Attacher <pid> <agentJarPath> [agentArgs]");
            System.exit(2);
        }
        String pid = args[0], jar = args[1], agentArgs = args.length > 2 ? args[2] : "";
        VirtualMachine vm = VirtualMachine.attach(pid);
        try {
            vm.loadAgent(jar, agentArgs);                 // → agent's agentmain(agentArgs, inst)
        } finally {
            vm.detach();
        }
        System.out.println("[vard] attached agent to pid " + pid);
    }
}
