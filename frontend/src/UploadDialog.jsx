import React from 'react';
import {Upload, X} from 'lucide-react';

export default function UploadDialog({files,multimodal,onChange,onClose,onUpload}) {
  return <div className="modal-backdrop"><section className="upload-dialog" role="dialog" aria-modal="true" aria-label="文档导入选项"><div className="drawer-head"><span><Upload size={19}/>导入 {files.length} 个文件</span><button onClick={onClose} aria-label="取消导入"><X size={19}/></button></div><ul className="upload-files">{files.map((f,i)=><li key={i}>{f.name}<span>{(f.size/1024).toFixed(1)} KB</span></li>)}</ul><label className={`multimodal-option ${multimodal?'enabled':''}`}><input type="checkbox" checked={multimodal} onChange={e=>onChange(e.target.checked)}/><span><b>启用多模态入库</b><small>让 DeepSeek 分析图片；保留布料、实物及复杂图形，并将原图关联到对应文段。</small></span></label><p className="upload-privacy">默认关闭。开启后，文档图片及邻近文字会发送到 DeepSeek，产生额外调用费用；无图片时自动使用普通入库流程，不调用视觉模型。图像描述为模型生成，不能代替检测结论。</p><div className="upload-dialog-actions"><button onClick={onClose}>取消</button><button className="primary" onClick={onUpload}><Upload size={16}/>开始导入</button></div></section></div>;
}
