// Read only the current-history CSVs in a standard Letterboxd ZIP.
const filenames=['diary.csv','reviews.csv','ratings.csv','watched.csv','watchlist.csv'];
const maxBytes=10*1024*1024;
const decoder=new TextDecoder('utf-8',{fatal:true});
const broken=()=>Error('Could not read this ZIP. Choose the original Letterboxd export.');
const checksums=Uint32Array.from({length:256},(_,value)=>{
  for(let bit=0;bit<8;bit++)value=(value>>>1)^((value&1)?0xedb88320:0);
  return value;
});
function crc32(bytes){
  let value=0xffffffff;
  for(const byte of bytes)value=(value>>>8)^checksums[(value^byte)&255];
  return (value^0xffffffff)>>>0;
}

async function inflate(bytes,size){
  let stream;
  try{stream=new DecompressionStream('deflate-raw');}
  catch{throw Error('This browser cannot open compressed ZIPs. Extract the export and select its CSVs.');}
  const reader=new Blob([bytes]).stream().pipeThrough(stream).getReader();
  const out=new Uint8Array(size);
  let offset=0;
  try{
    while(true){
      const {done,value}=await reader.read();
      if(done)break;
      if(offset+value.length>size)throw broken();
      out.set(value,offset);
      offset+=value.length;
    }
    if(offset!==size)throw broken();
  }catch{
    await reader.cancel().catch(()=>{});
    throw broken();
  }finally{reader.releaseLock();}
  return out;
}

export async function readLetterboxdZip(buffer){
  if(buffer.byteLength<22)throw broken();
  if(buffer.byteLength>maxBytes)throw Error('Choose a Letterboxd ZIP up to 10 MB.');
  const bytes=new Uint8Array(buffer);
  const view=new DataView(buffer);
  const word=offset=>view.getUint16(offset,true);
  const dword=offset=>view.getUint32(offset,true);
  let end=bytes.length-22;
  const lower=Math.max(0,end-65535);
  for(;end>=lower;end--){
    if(dword(end)===0x06054b50&&end+22+word(end+20)===bytes.length)break;
  }
  if(end<lower)throw broken();
  const count=word(end+10), directory=dword(end+16);
  if(word(end+4)||word(end+6)||word(end+8)!==count||count>3000||directory+dword(end+12)!==end)throw broken();

  const records=new Map();
  let position=directory, expanded=0;
  for(let i=0;i<count;i++){
    if(position+46>end||dword(position)!==0x02014b50)throw broken();
    const nameLength=word(position+28);
    const next=position+46+nameLength+word(position+30)+word(position+32);
    if(next>end)throw broken();
    let path='';
    try{path=decoder.decode(bytes.subarray(position+46,position+46+nameLength));}catch{}
    // Ignore deleted content, profile data, and private lists.
    const parts=path.split('/');
    const name=parts.at(-1).toLowerCase();
    const currentHistory=parts.length===1||(parts.length===2&&/^letterboxd[-_]/i.test(parts[0]));
    if(currentHistory&&filenames.includes(name)){
      if(records.has(name)||word(position+34))throw broken();
      const flags=word(position+8), method=word(position+10), compressed=dword(position+20);
      const size=dword(position+24), local=dword(position+42), crc=dword(position+16);
      if((flags&1)||![0,8].includes(method)||local+30>directory||dword(local)!==0x04034b50||
         word(local+6)!==flags||word(local+8)!==method)throw broken();
      const start=local+30+word(local+26)+word(local+28);
      if(start+compressed>directory||word(local+26)!==nameLength||
         !bytes.subarray(local+30,local+30+nameLength).every((byte,index)=>byte===bytes[position+46+index]))throw broken();
      expanded+=size;
      if(size>maxBytes||expanded>2*maxBytes)throw Error('This ZIP expands beyond the import limit. Select the CSV files in smaller batches.');
      records.set(name,{start,compressed,size,method,crc});
    }
    position=next;
  }
  if(position!==end||!records.size)throw Error('No Letterboxd history CSVs found in this ZIP.');
  const files=[];
  for(const name of filenames){
    const record=records.get(name);
    if(!record)continue;
    const compressed=bytes.subarray(record.start,record.start+record.compressed);
    const raw=record.method===0?compressed:await inflate(compressed,record.size);
    if(raw.length!==record.size||crc32(raw)!==record.crc)throw broken();
    try{files.push({name,text:decoder.decode(raw)});}catch{throw broken();}
  }
  return files;
}
