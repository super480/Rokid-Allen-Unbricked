package cn.rokid.bridge;

import android.content.*;
import android.os.*;

/** Uses transaction 8 and ITTSCallbackEx, matching this firmware, not guessed AIDL. */
final class RokidTts {
    interface Listener { void state(String state, int id, int error); }
    private final Context context;
    private final Listener listener;
    private volatile IBinder remote;
    private boolean bound;
    private final Binder callback;
    private final ServiceConnection connection;

    RokidTts(Context c, final Listener l) {
        context=c; listener=l;
        callback=new Binder() {
            protected boolean onTransact(int code, Parcel d, Parcel r, int flags) throws RemoteException {
                if(code==INTERFACE_TRANSACTION){ if(r!=null)r.writeString("rokid.os.ITTSCallbackEx");return true; }
                if(code<1||code>4)return super.onTransact(code,d,r,flags);
                d.enforceInterface("rokid.os.ITTSCallbackEx"); int id=d.readInt(); int error=code==4?d.readInt():0;
                listener.state(code==1?"speaking":code==2?"playback_stopped":code==3?"playback_done":"tts_error",id,error);
                if(r!=null){r.writeNoException();} return true;
            }
        };
        connection=new ServiceConnection(){
            public void onServiceConnected(ComponentName n,IBinder b){
                try{if(!"rokid.os.IR2TTS".equals(b.getInterfaceDescriptor()))throw new SecurityException("Unexpected TTS descriptor");remote=b;listener.state("tts_connected",0,0);}
                catch(Exception e){listener.state("tts_error",0,-1);}
            }
            public void onServiceDisconnected(ComponentName n){remote=null;listener.state("tts_disconnected",0,0);}
        };
    }
    void connect(){
        Intent i=new Intent();i.setComponent(new ComponentName("com.rokid.service.rktts","com.rokid.service.rktts.RKTTSService"));
        bound=context.bindService(i,connection,Context.BIND_AUTO_CREATE);
        if(!bound)listener.state("tts_error",0,-2);
    }
    /**
     * Rebinds after the service process died.
     *
     * The factory's TTS service is PERSISTENT, so the system restarts it on
     * its own. Android drops our binding with the dead process, and the
     * fresh process comes up with an empty engine. Rebinding picks it up.
     */
    void reconnect(){
        close();
        connect();
    }
    int speak(String text)throws Exception{
        if(remote==null)throw new IllegalStateException("TTS not connected");
        Parcel d=Parcel.obtain(),r=Parcel.obtain();
        try{d.writeInterfaceToken("rokid.os.IR2TTS");d.writeString(text);d.writeStrongBinder(callback);remote.transact(8,d,r,0);r.readException();return r.readInt();}
        finally{d.recycle();r.recycle();}
    }
    boolean cancelRequest(int id)throws Exception{
        IBinder target=remote;
        if(target==null||id<=0)return false;
        Parcel d=Parcel.obtain(),r=Parcel.obtain();
        try{d.writeInterfaceToken("rokid.os.IR2TTS");d.writeInt(id);if(!target.transact(10,d,r,0))return false;r.readException();return true;}
        finally{d.recycle();r.recycle();}
    }
    void close(){if(bound){context.unbindService(connection);bound=false;}remote=null;}
}
