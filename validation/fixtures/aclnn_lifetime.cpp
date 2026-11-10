// A deferred host queue: operands leave their caller's scope before execution.
#include <algorithm>
#include <atomic>
#include <cstdint>
#include <functional>
#include <map>
#include <memory>
#include <stdexcept>
#include <string>
#include <thread>
#include <tuple>
#include <vector>
#define TORCH_CHECK(condition, ...) if (!(condition)) throw std::runtime_error(#condition)
thread_local bool initialized = false;
std::atomic<int> alive{0}, releases{0}, completions{0};
struct Buffer { Buffer(){++alive;} ~Buffer(){--alive;} };
namespace at {
constexpr int kByte = 0;
struct TensorOptions { explicit TensorOptions(int){} TensorOptions dtype(int){return *this;} };
struct Tensor { std::shared_ptr<Buffer> buffer; void *data_ptr(){return buffer.get();} };
Tensor empty(std::initializer_list<int64_t>, TensorOptions){return {std::make_shared<Buffer>()};}
}
namespace torch_npu { namespace utils {int get_npu_device_type(){return 0;}} }
using aclrtStream = void *;
struct aclOpExecutor { Buffer *operand; };
namespace c10_npu {
static void *expected_stream = reinterpret_cast<void*>(17);
struct Stream { void *stream(bool){return expected_stream;} }; Stream getCurrentNPUStream(){return {};}
namespace NPUCachingAllocator {
void *raw_alloc_with_stream(size_t bytes, aclrtStream stream){
 TORCH_CHECK(bytes==16 && stream==expected_stream); return new Buffer;
}
void raw_delete(void *ptr){delete static_cast<Buffer*>(ptr);}
}
}
std::vector<std::function<int()>> queue;
namespace at_npu { namespace native {
struct OpCommand {
 std::function<int()> handler;
 void Name(const char*){}
 void SetCustomHandler(std::function<int()> f){handler=std::move(f);}
 void Run(){queue.push_back(handler);}
};
} }
void init(void*,bool){TORCH_CHECK(!initialized); initialized=true;}
void release(void*,bool){TORCH_CHECK(initialized); ++releases;}
void uninit(void*,bool){TORCH_CHECK(initialized); initialized=false;}
int workspace(Buffer *operand, uint64_t *size, aclOpExecutor **executor){
 TORCH_CHECK(initialized); TORCH_CHECK(alive==1); *size=16; *executor=new aclOpExecutor{operand}; return 0;
}
int execute(void *buffer,uint64_t size,aclOpExecutor *executor,aclrtStream stream){
 TORCH_CHECK(stream==c10_npu::expected_stream);
 TORCH_CHECK(initialized); TORCH_CHECK(buffer && size==16 && executor->operand);
 TORCH_CHECK(alive==2); delete executor; ++completions; return 0;
}
void *GetOpApiFuncAddr(const char *name){
 std::string s(name);
 if(s=="InitHugeMemThreadLocal")return reinterpret_cast<void*>(init);
 if(s=="UnInitHugeMemThreadLocal")return reinterpret_cast<void*>(uninit);
 if(s=="ReleaseHugeMem")return reinterpret_cast<void*>(release);
 if(s=="aclnnProbeGetWorkspaceSize")return reinterpret_cast<void*>(workspace);
 return reinterpret_cast<void*>(execute);
}
const char *GetOpApiLibName(){return "probe";}
const char *aclGetRecentErrMsg(){return "probe failure";}
using InitHugeMemThreadLocal=void(*)(void*,bool);
using UnInitHugeMemThreadLocal=void(*)(void*,bool);
using ReleaseHugeMem=void(*)(void*,bool);
template<class... T> auto OwnApiParameters(const T &... args){return std::make_tuple(args...);}
auto ConvertTypes(at::Tensor &t,uint64_t *&n,aclOpExecutor **&e){return std::make_tuple(t.buffer.get(),n,e);}
template<class T> auto ConvertToOpApiFunc(T&,void *p){return reinterpret_cast<int(*)(Buffer*,uint64_t*,aclOpExecutor**)>(p);}
template<class F,class T> auto call(F f,T &t){return std::apply(f,t);}
template<class T> void ReleaseConvertTypes(T&){}
// EXEC_NPU_CMD is inserted from the production header by the Python test.
PRODUCTION_MACRO
int main(){
 {
  at::Tensor operand{std::make_shared<Buffer>()};
  EXEC_NPU_CMD(aclnnProbe,operand);
 }
 TORCH_CHECK(completions==0 && alive==1 && !initialized);
 std::exception_ptr error;
 std::thread worker([&]{try {queue.front()(); TORCH_CHECK(!initialized);} catch(...){error=std::current_exception();}});
 worker.join(); if(error)std::rethrow_exception(error);
 queue.clear(); TORCH_CHECK(completions==1 && releases==1 && alive==0);
}
