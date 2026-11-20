package cn.rokid.bridge;

import android.os.*;
import java.lang.reflect.Method;
import java.util.ArrayList;

/** Wire format independently implemented from the device's NativeRKEventBusProxy/RKEvent. */
final class RokidBus {
    interface Listener { void event(String action, int flags, Bundle data); }
    private IBinder remote;
    private final ArrayList<String> actions = new ArrayList<String>();
    private static final String SERVICE_TOKEN = "rokid.os.IEventBus";
    private static final String CALLBACK_TOKEN = "rokid.os.EventBus.S";
    private final Binder callback;

    RokidBus(final Listener listener) {
        callback = new Binder() {
            protected boolean onTransact(int code, Parcel data, Parcel reply, int flags) throws RemoteException {
                if (code == INTERFACE_TRANSACTION) { if (reply != null) reply.writeString(CALLBACK_TOKEN); return true; }
                if (code != 1) return super.onTransact(code, data, reply, flags);
                data.enforceInterface(CALLBACK_TOKEN);
                String action = data.readString();
                int eventFlags = data.readInt();
                Bundle extra = data.readBundle(getClass().getClassLoader());
                try { listener.event(action, eventFlags, extra == null ? new Bundle() : extra); }
                catch (Exception e) { android.util.Log.e("Laokou", "bus callback", e); }
                // Observer only: never claim consumption or block original routing.
                if ((flags & IBinder.FLAG_ONEWAY) == 0 && reply != null) { reply.writeNoException(); reply.writeInt(0); }
                return true;
            }
        };
    }

    static IBinder lookup(String name) throws Exception {
        Method m = Class.forName("android.os.ServiceManager").getMethod("getService", String.class);
        return (IBinder)m.invoke(null, name);
    }

    String connect(String[] wanted) throws Exception {
        remote = lookup("rk_event_bus");
        if (remote == null) throw new IllegalStateException("rk_event_bus not available");
        String descriptor = remote.getInterfaceDescriptor();
        if (!SERVICE_TOKEN.equals(descriptor)) throw new SecurityException("Unexpected event bus: " + descriptor);
        Parcel data = Parcel.obtain(), reply = Parcel.obtain();
        try {
            data.writeInterfaceToken(SERVICE_TOKEN); data.writeStrongBinder(callback); data.writeInt(100);
            if (!remote.transact(1, data, reply, 0)) throw new IllegalStateException("register not implemented");
            reply.readException();
        } finally { data.recycle(); reply.recycle(); }
        for (String action : wanted) {
            transactAction(3, action); actions.add(action);
        }
        transactAction(3,"cn.rokid.bridge.PROBE"); actions.add("cn.rokid.bridge.PROBE");
        Parcel probe=Parcel.obtain();
        try{probe.writeInterfaceToken(SERVICE_TOKEN);probe.writeInt(1);probe.writeString("cn.rokid.bridge.PROBE");probe.writeInt(1);Bundle b=new Bundle();b.putString("probe","self-test");probe.writeBundle(b);remote.transact(2,probe,null,IBinder.FLAG_ONEWAY);}
        finally{probe.recycle();}
        return descriptor;
    }

    private void transactAction(int code, String action) throws Exception {
        Parcel d = Parcel.obtain(), r = Parcel.obtain();
        try { d.writeInterfaceToken(SERVICE_TOKEN); d.writeString(action); remote.transact(code,d,r,0); r.readException(); }
        finally { d.recycle(); r.recycle(); }
    }

    void close() {
        for (String action : actions) { try { transactAction(4, action); } catch (Exception ignored) {} }
        actions.clear(); remote = null;
    }
}
